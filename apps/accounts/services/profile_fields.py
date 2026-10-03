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

from django.utils import timezone

from apps.accounts.models import AnswerBank

# Whole-field keys: a single provenance entry covers the entire value.
SIMPLE_FIELDS = (
    "full_name",
    "phone",
    "current_employer",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "target_tags",
    "citizenship_countries",
)
# Dict-valued fields: one provenance entry per key, e.g.
# "visa_status_by_country.USA".
KEYED_FIELDS = ("visa_status_by_country", "salary_by_region")
COVERED_FIELDS = SIMPLE_FIELDS + KEYED_FIELDS

_SOURCE_RANK = {
    AnswerBank.Source.IMPORTED: 1,
    AnswerBank.Source.LEARNED: 2,
    AnswerBank.Source.USER: 3,
}
_LOCKED_RANK = 4


def _is_empty(value):
    return value in (None, "", [], {})


def _entry(source, *, locked=False, detail=""):
    return {
        "source": str(source),
        "locked": locked,
        "updated_at": timezone.now().isoformat(),
        "detail": detail,
    }


def _rank(entry):
    if entry.get("locked"):
        return _LOCKED_RANK
    return _SOURCE_RANK.get(entry.get("source"), _SOURCE_RANK[AnswerBank.Source.USER])


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
    if _is_empty(value):
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
            if _is_empty(new):
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
    if existing is not None and _rank(existing) >= _SOURCE_RANK[source]:
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
