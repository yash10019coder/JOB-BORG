"""Provenance bookkeeping for plain Profile columns (FR7.10).

``Profile.field_provenance`` records who wrote each covered field so a
re-sync, importer or the learner can never silently overwrite what the user
typed. This module is the only writer of that JSON.

Two entry points:

* :func:`record_user_edits` -- the user (form/admin) changed fields. Mutates
  the instance in memory only; the caller's own single ``save()`` persists it,
  so no extra ``post_save`` (and therefore no extra rematch) is triggered.
* :func:`apply_profile_field` -- an automated writer (importer/learner)
  proposes a value. Returns ``False`` and writes nothing when a higher-
  precedence source already holds the field.

Precedence: user-locked > user-set > learned > imported.
"""
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dc_field

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AnswerBank, Profile
from apps.accounts.services.precedence import LOCKED_RANK, SOURCE_RANK, is_blank

# Whole-field keys: a single provenance entry covers the entire value.
SIMPLE_FIELDS = (
    "full_name",
    "headline",
    "phone",
    "current_employer",
    "location_city",
    "location_country",
    "mailing_address",
    "working_timezone",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "target_titles",
    "target_tags",
    "citizenship_countries",
)
# Dict-valued fields: one provenance entry per key, e.g.
# "visa_status_by_country.USA".
KEYED_FIELDS = ("visa_status_by_country", "salary_by_region")
COVERED_FIELDS = SIMPLE_FIELDS + KEYED_FIELDS

def _entry(source, *, locked=False, detail=""):
    return {
        "source": str(source),
        "locked": locked,
        "updated_at": timezone.now().isoformat(),
        "detail": detail,
    }


def _rank(entry):
    if entry.get("locked"):
        return LOCKED_RANK
    return SOURCE_RANK.get(entry.get("source"), SOURCE_RANK[AnswerBank.Source.USER])


def snapshot(profile):
    """Deep copy of the covered values, taken before a form edits them."""
    return {field: deepcopy(getattr(profile, field)) for field in COVERED_FIELDS}


def provenance_for(profile, key):
    """The effective provenance entry for ``key`` (a field or ``field.subkey``).

    A missing entry on a non-empty value is legacy data and counts as the
    user's; an empty value has no provenance (``None``).
    """
    entry = (profile.field_provenance or {}).get(key)
    if entry:
        return entry
    field, _, subkey = key.partition(".")
    value = getattr(profile, field)
    if subkey:
        value = (value or {}).get(subkey)
    if is_blank(value):
        return None
    return _entry(AnswerBank.Source.USER)


def _keys_for(field, value):
    if field in KEYED_FIELDS:
        return {f"{field}.{k}" for k in (value or {})}
    return {field}


def record_user_edits(profile, before):
    """Stamp ``source=user`` on every covered field/key that differs from
    ``before`` (a :func:`snapshot`, or an equivalent dict). Existing lock
    flags are preserved. In-memory only; the caller saves once.
    """
    provenance = dict(profile.field_provenance or {})
    for field in COVERED_FIELDS:
        if field not in before:
            continue
        old, new = before[field], getattr(profile, field)
        if field in KEYED_FIELDS:
            old, new = old or {}, new or {}
            changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
            for key in changed:
                full = f"{field}.{key}"
                if key in new:
                    locked = bool(provenance.get(full, {}).get("locked"))
                    provenance[full] = _entry(AnswerBank.Source.USER, locked=locked)
                else:
                    provenance.pop(full, None)
        elif old != new:
            if is_blank(new):
                provenance.pop(field, None)
            else:
                locked = bool(provenance.get(field, {}).get("locked"))
                provenance[field] = _entry(AnswerBank.Source.USER, locked=locked)
    profile.field_provenance = provenance


def apply_profile_field(profile, key, value, source, *, detail=""):
    """Write ``value`` to ``key`` on behalf of an automated ``source``.

    ``key`` is a covered field (``"phone"``) or ``"field.subkey"`` for the
    dict fields (``"salary_by_region.US"``). Returns ``True`` if written,
    ``False`` ("kept") if a user-set, locked or higher-precedence value is
    already there. One ``save()`` with explicit ``update_fields``.
    """
    source = AnswerBank.Source(source)
    if source == AnswerBank.Source.USER:
        raise ValueError("user edits go through record_user_edits() in the form save")
    field, _, subkey = key.partition(".")
    if field not in COVERED_FIELDS or bool(subkey) != (field in KEYED_FIELDS):
        raise ValueError(f"{key!r} is not a covered profile field")

    existing = provenance_for(profile, key)
    if existing is not None and _rank(existing) >= SOURCE_RANK[source]:
        return False

    if subkey:
        current = dict(getattr(profile, field) or {})
        current[subkey] = value
        setattr(profile, field, current)
    else:
        setattr(profile, field, value)
    provenance = dict(profile.field_provenance or {})
    provenance[key] = _entry(source, detail=detail)
    profile.field_provenance = provenance
    profile.save(update_fields=[field, "field_provenance", "updated_at"])
    return True


# ---------------------------------------------------------------------------
# Import (Phase 4): the only fields an importer may propose, and how they are
# written. ``apply_profile_field`` stays as the generic one-field writer; the
# importer needs its own because it writes several fields in ONE save (one
# rematch), may replace an earlier *imported* value (re-sync), and validates.
# ---------------------------------------------------------------------------
IMPORTABLE_FIELDS = (
    "full_name",
    "headline",
    "phone",
    "current_employer",
    "location_city",
    "location_country",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "target_titles",
    "target_tags",
)


class ImportState:
    """What the profile currently holds for an importable field."""

    EMPTY = "empty"
    IMPORTED = "imported"
    LEARNED = "learned"
    USER = "user"
    LOCKED = "locked"
    # A re-import may replace these; it must keep the rest (rule R2).
    REPLACEABLE = (EMPTY, IMPORTED)


class ImportApplyError(ValueError):
    """Some proposed values failed validation; nothing was written (rule V4).
    ``errors`` maps field -> short code, never the value."""

    def __init__(self, errors):
        super().__init__("invalid import values")
        self.errors = errors


@dataclass
class ApplyResult:
    applied: list = dc_field(default_factory=list)
    unchanged: list = dc_field(default_factory=list)
    kept: dict = dc_field(default_factory=dict)  # field -> ImportState it was kept for


def import_state(profile, key):
    """Where the current value of ``key`` came from (see :class:`ImportState`)."""
    entry = provenance_for(profile, key)
    if entry is None:
        return ImportState.EMPTY
    if entry.get("locked"):
        return ImportState.LOCKED
    return entry.get("source") or ImportState.USER


def apply_imported(profile_id, values, *, detail="", edited=()):
    """Write accepted import ``values`` ({field: value}) in one transaction.

    * Only :data:`IMPORTABLE_FIELDS`; anything else (a T0/T1 field such as
      ``visa_status_by_country``) raises ``ValueError``.
    * Every value is validated first; any failure raises
      :class:`ImportApplyError` and writes nothing (V4).
    * The row is locked and the state re-checked, so a field the user changed in
      another tab since the review page rendered is *kept* (R5).
    * A user-set, learned or locked field is kept; an empty or previously
      imported one is written. A value in ``edited`` was changed by the user on
      the review page, so it is stamped ``user`` instead of ``imported`` (R3).
    * One ``save`` with the union of ``update_fields`` (R4): at most one rematch.
    """
    from apps.accounts.importing.validators import ImportValueError, clean_field

    unknown = set(values) - set(IMPORTABLE_FIELDS)
    if unknown:
        raise ValueError(f"not importable: {sorted(unknown)}")

    cleaned, errors = {}, {}
    for key, raw in values.items():
        try:
            cleaned[key] = clean_field(key, raw)
        except ImportValueError as exc:
            errors[key] = exc.code
    if errors:
        raise ImportApplyError(errors)

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(pk=profile_id)
        result = ApplyResult()
        provenance = dict(profile.field_provenance or {})
        for key, value in cleaned.items():
            state = import_state(profile, key)
            if state not in ImportState.REPLACEABLE:
                result.kept[key] = state
                continue
            if state == ImportState.IMPORTED and getattr(profile, key) == value:
                result.unchanged.append(key)
                continue
            setattr(profile, key, value)
            source = AnswerBank.Source.USER if key in edited else AnswerBank.Source.IMPORTED
            provenance[key] = _entry(source, detail="" if source == AnswerBank.Source.USER else detail)
            result.applied.append(key)
        if result.applied:
            profile.field_provenance = provenance
            profile.save(update_fields=[*result.applied, "field_provenance", "updated_at"])
    return result
