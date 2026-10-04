"""Answer resolution with provenance (FR7.3, FR7.4, FR7.7, FR7.8).

``resolve_answer`` looks up the answer to one application question and says
where it came from and whether the user still has to confirm it before it may
be submitted. Resolution order today:

1. ``AnswerBank`` row for the normalized question key.
2. The injected ``legacy_lookup`` (the old ``ExplicitAnswer`` table, supplied
   by ``apps.auto_apply`` so this module never imports it).

Typed Profile facts (visa / citizenship / salary by region) join the front of
that order in Phase 2; ``job`` is accepted now so the signature is stable.

The question's risk tier is recomputed on every call and the riskier of the
stored and computed tier wins, so a classifier fix takes effect on rows that
were written under the old rules. A T0/T1 value is *confirmed* only when its
source is ``user``; anything learned or imported needs the user's explicit
confirmation (``needs_confirmation``) before submission.

``write_answer`` is the one writer of ``AnswerBank``: it enforces precedence
(user-locked > user-set > learned > imported) and keeps the superseded value
in ``AnswerBankHistory``.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts import question_semantics as qs
from apps.accounts.models import AnswerBank, AnswerBankHistory
from apps.accounts.services import profile_fields, typed_facts
from apps.accounts.services.precedence import LOCKED_RANK, SOURCE_RANK
from apps.accounts.tiering import QuestionCategory, Tier, classify, classify_tier, higher_tier

logger = logging.getLogger(__name__)

# FR7.7: learned T2 answers expire after 180 days, imported after 365, unless
# re-confirmed (any write refreshes ``updated_at``). Assumptions pending real
# answer-change data (see Open Questions in the requirements doc).
LEARNED_T2_TTL = timedelta(days=180)
IMPORTED_T2_TTL = timedelta(days=365)

_MAX_KEY_LEN = 255
_NEEDS_CONFIRMATION_TIERS = {Tier.T0_LEGAL, Tier.T1_COMMERCIAL}


@dataclass(frozen=True)
class ResolvedValue:
    value: Any
    provenance: dict = field(default_factory=dict)
    needs_confirmation: bool = False
    tier: str = Tier.T0_LEGAL


@dataclass(frozen=True)
class WriteResult:
    applied: bool
    row: AnswerBank | None


_OPTIONAL_MARKER = re.compile(r"\((?:optional|required)\)")


def normalize_question_key(question_text, employer=None):
    """Stable key for a question: lowercased, with decoration removed.

    Decoration only -- a trailing ``*``, "(optional)"/"(required)", a leading
    "please", punctuation and whitespace -- plus the employer's own name when
    given (so "relocate to work at Acme" and "...at Beta" are one question).
    Verbs are deliberately kept ("do you have X" is not "are you X"): a false
    merge is worse than a miss.

    The option set is *not* part of the key. The stored value is mapped onto
    each form's live options when it is resolved, so the same fact is found
    whatever a particular ATS calls its choices.
    """
    text = (question_text or "").lower()
    name = (employer or "").strip().lower()
    if name:
        text = re.sub(rf"\b{re.escape(name)}(?:'s)?\b", " ", text)
    text = _OPTIONAL_MARKER.sub(" ", text)
    base = re.sub(r"[^\w]+", " ", text).strip()
    base = re.sub(r"^please\s+", "", base)
    if len(base) > _MAX_KEY_LEN:
        digest = hashlib.sha1(base.encode()).hexdigest()[:10]
        base = f"{base[: _MAX_KEY_LEN - 11]}~{digest}"
    return base


def options_hash(options):
    """Short digest of a form's option set, kept in ``source_detail`` for audit."""
    if not options:
        return ""
    return hashlib.sha1("\x1f".join(sorted(map(str, options))).encode()).hexdigest()[:10]


def _expires_at(row):
    if row.expires_at is not None:
        return row.expires_at
    if row.risk_tier != Tier.T2_FACTUAL:
        return None
    if row.source == AnswerBank.Source.LEARNED:
        return row.updated_at + LEARNED_T2_TTL
    if row.source == AnswerBank.Source.IMPORTED:
        return row.updated_at + IMPORTED_T2_TTL
    return None


def _is_expired(row, now):
    expires_at = _expires_at(row)
    return expires_at is not None and expires_at <= now


def _rank(row):
    return LOCKED_RANK if row.is_locked else SOURCE_RANK[row.source]


def load_bank_rows(profile):
    """All of a profile's ``AnswerBank`` rows keyed by
    ``(question_key, scope_region)``, for callers that resolve many questions
    (one query instead of one each)."""
    if profile is None:
        return {}
    return {
        (row.question_key, row.scope_region): row
        for row in AnswerBank.objects.filter(profile=profile)
    }


def _employer_name(job):
    """The job's employer name, or ``None`` (duck-typed: this module does not
    import the jobs app)."""
    return getattr(getattr(job, "employer", None), "name", None)


# Rows backfilled from the old ExplicitAnswer table (Phase 2, auto_apply
# migration 0011) live in AnswerBank under these literal keys. Only the
# resolver reads them, routed by what the question asks.
LEGACY_WORK_AUTHORIZATION = "legacy:work_authorization"
LEGACY_SPONSORSHIP = "legacy:sponsorship"
LEGACY_SALARY = "legacy:salary_expectation"


def _get_row(profile, key, scope, bank_rows):
    if bank_rows is not None:
        return bank_rows.get((key, scope))
    return AnswerBank.objects.filter(
        profile=profile, question_key=key, scope_region=scope
    ).first()


def _bank_row(profile, question_text, key, job, bank_rows):
    """The AnswerBank row that applies to this question for this job.

    A location-sensitive question (relocation, on-site, a named country) is
    answered only from a row saved for the job's own region, or one the user
    marked "applies everywhere" -- an answer given for a US job is never
    silently reused for an India job. Everything else uses the global row.
    """
    if not qs.is_location_sensitive(question_text):
        return _get_row(profile, key, "", bank_rows)
    region = typed_facts.job_region(job)
    row = _get_row(profile, key, region, bank_rows) if region else None
    if row is None:
        row = _get_row(profile, key, "", bank_rows)
        if row is not None and not (row.source_detail or {}).get("applies_everywhere"):
            row = None
    return row


def _map_to_options(value, options):
    """Put a stored value into a form's live options, or ``None``.

    Exact (case/punctuation-insensitive) match, or a plain "Yes"/"No" mapped to
    the single option that means it. Anything else is not guessed: a stored
    answer that does not fit this form's choices stays blank for review.
    """
    if not options:
        return value
    if isinstance(value, (list, tuple)):
        mapped = [qs.pick_exact(options, item) for item in value]
        return mapped if mapped and all(m is not None for m in mapped) else None
    exact = qs.pick_exact(options, value)
    if exact is not None:
        return exact
    word = re.sub(r"[^a-z]+", "", str(value).lower())
    if word in ("yes", "no"):
        return qs.pick_yes_no(options, word == "yes")
    return None


def _bank_provenance(row):
    return {
        "origin": "answer_bank",
        "source": row.source,
        "locked": row.is_locked,
        "confidence": row.confidence,
        "detail": row.source_detail,
    }


def _typed_result(profile, fact, tier, conflict):
    entry = profile_fields.provenance_for(profile, fact.provenance_key) or {}
    source = entry.get("source", AnswerBank.Source.USER)
    provenance = {
        "origin": f"profile.{fact.provenance_key}",
        "source": source,
        "locked": bool(entry.get("locked")),
        "confidence": 1.0,
        "detail": fact.detail,
    }
    if conflict:
        provenance["conflict"] = conflict
    return ResolvedValue(
        value=fact.value,
        provenance=provenance,
        needs_confirmation=(
            tier in _NEEDS_CONFIRMATION_TIERS and source != AnswerBank.Source.USER
        ),
        tier=tier,
    )


def _legacy_key(question_text):
    if classify(question_text) == QuestionCategory.WORK_AUTHORIZATION:
        kind = qs.authorization_kind(question_text)
        if kind == qs.AuthKind.AUTHORIZATION:
            return LEGACY_WORK_AUTHORIZATION
        if kind == qs.AuthKind.SPONSORSHIP:
            return LEGACY_SPONSORSHIP
        return None
    if qs.is_salary_expectation(question_text):
        return LEGACY_SALARY
    return None


def _legacy_conflict(profile, fact, bank_rows):
    """A backfilled legacy answer that disagrees with the typed fact, if any.

    Non-blocking: legacy answers are country-agnostic while typed facts are
    per country, so they can legitimately differ, and both came from the user.
    It is logged and carried in the provenance so the queue can say so.
    """
    if fact.truth is None or fact.kind not in (typed_facts.AUTHORIZATION, typed_facts.SPONSORSHIP):
        return None
    lkey = LEGACY_WORK_AUTHORIZATION if fact.kind == typed_facts.AUTHORIZATION else LEGACY_SPONSORSHIP
    row = _get_row(profile, lkey, "", bank_rows)
    boolean = ((row.source_detail or {}).get("answer_bool")) if row is not None else None
    if boolean is None or boolean == fact.truth:
        return None
    logger.info(
        "answer_resolver.legacy_conflict",
        extra={"profile_id": profile.pk, "legacy_key": lkey, "kind": fact.kind},
    )
    return {"origin": lkey, "answer_bool": boolean}


def _legacy_row(profile, question_text, job, options, bank_rows, computed_tier, now):
    lkey = _legacy_key(question_text)
    if lkey is None:
        return None
    row = _get_row(profile, lkey, "", bank_rows)
    if row is None or _is_expired(row, now):
        return None
    detail = row.source_detail or {}
    if lkey == LEGACY_SALARY:
        # A band is quoted in one region's currency: never answer another's.
        region = typed_facts.job_region(job)
        if not region or detail.get("region") != region:
            return None
        value = _map_to_options(row.value, options)
    elif detail.get("answer_bool") is not None:
        value = qs.pick_yes_no(options, bool(detail["answer_bool"]))
    elif detail.get("free_text"):
        value = _map_to_options(row.value, options)
    else:
        return None  # the old answer was "other": it never answered anything
    if value is None:
        return None
    # Counted so Phase 5 can tell when the legacy rows are unused.
    logger.info(
        "answer_resolver.legacy_fallback_hit",
        extra={"profile_id": profile.pk, "question_key": lkey},
    )
    provenance = _bank_provenance(row)
    provenance["origin"] = "legacy_explicit_answer"
    return ResolvedValue(
        value=value,
        provenance=provenance,
        needs_confirmation=False,
        tier=higher_tier(row.risk_tier, computed_tier),
    )


def resolve_answer(
    profile,
    question_text,
    job=None,
    *,
    options=(),
    legacy_lookup: Callable[[str], Any] | None = None,
    bank_rows: dict | None = None,
    strip_employer: bool = True,
    now=None,
):
    """Return a :class:`ResolvedValue`, or ``None`` if nothing is known.

    ``strip_employer=False`` keeps the employer's name in the key: for a
    narrative (long-text) question the answer is about *this* employer and must
    never be matched to another's.

    Order: typed Profile facts, then AnswerBank (region-aware), then the
    backfilled legacy rows -- but legacy only when no typed fact covers the
    question, so an H-1B holder's sponsorship stays blank rather than being
    filled from an old saved answer. A returned ``value`` of ``None`` means a
    stored answer exists but does not fit this form's options.

    ``bank_rows`` is an optional :func:`load_bank_rows` preload; when given, no
    per-question query is made. ``legacy_lookup`` is the pre-backfill
    ``ExplicitAnswer`` hook, kept until that table is retired.
    """
    now = now or timezone.now()
    options = tuple(options or ())
    key = normalize_question_key(
        question_text, _employer_name(job) if strip_employer else None
    )
    computed_tier = classify_tier(question_text)

    fact = typed_facts.resolve(profile, question_text, options=options, job=job)
    if fact is not None and fact.value is not None:
        conflict = _legacy_conflict(profile, fact, bank_rows)
        return _typed_result(profile, fact, computed_tier, conflict)

    row = _bank_row(profile, question_text, key, job, bank_rows) if profile is not None else None
    if row is not None and not _is_expired(row, now):
        tier = higher_tier(row.risk_tier, computed_tier)
        value = _map_to_options(row.value, options)
        provenance = _bank_provenance(row)
        if value is None:
            provenance["unmapped_value"] = row.value
        return ResolvedValue(
            value=value,
            provenance=provenance,
            needs_confirmation=(
                tier in _NEEDS_CONFIRMATION_TIERS and row.source != AnswerBank.Source.USER
            ),
            tier=tier,
        )

    if profile is not None and not (fact and fact.covered):
        found = _legacy_row(profile, question_text, job, options, bank_rows, computed_tier, now)
        if found is not None:
            return found

    if legacy_lookup is not None and not (fact and fact.covered):
        legacy_value = legacy_lookup(question_text)
        if legacy_value is not None:
            # Counted so Phase 5 can tell when the legacy table is unused.
            logger.info(
                "answer_resolver.legacy_fallback_hit",
                extra={"profile_id": getattr(profile, "pk", None), "question_key": key},
            )
            return ResolvedValue(
                value=legacy_value,
                provenance={
                    "origin": "legacy_explicit_answer",
                    "source": AnswerBank.Source.USER,
                    "locked": False,
                    "confidence": 1.0,
                    "detail": {},
                },
                needs_confirmation=False,
                tier=computed_tier,
            )
    return None


def _locked_row(profile, key, scope_region=""):
    return (
        AnswerBank.objects.select_for_update()
        .filter(profile=profile, question_key=key, scope_region=scope_region)
        .first()
    )


def write_answer(
    profile,
    question_text,
    value,
    source,
    *,
    options=(),
    category=None,
    source_detail=None,
    confidence=1.0,
    is_locked=False,
    scope_region="",
    employer=None,
    min_tier=None,
    applies_everywhere=False,
    now=None,
):
    """Create or replace the ``AnswerBank`` row for a question.

    Returns ``WriteResult(applied=False, row=<existing>)`` when a higher-
    precedence row already holds the question -- the caller may then propose
    the value as a suggestion instead. A replaced value is kept in
    ``AnswerBankHistory`` in the same transaction.

    An *expired* row no longer outranks anyone (it resolves to nothing, so it
    must not block a writer either). ``category=None`` keeps the existing
    row's category on update and means ``other`` on create.

    ``scope_region`` limits the answer to one region ("" = everywhere);
    rows for the same question in different regions coexist. ``employer``
    names the employer to strip from the key. ``min_tier`` lets the user
    raise (never lower) the question's risk tier. ``options`` is recorded as
    a hash in ``source_detail`` for audit but is not part of the key.
    """
    source = AnswerBank.Source(source)
    if is_locked and source != AnswerBank.Source.USER:
        raise ValueError("only the user may lock an answer")
    now = now or timezone.now()
    key = normalize_question_key(question_text, employer)
    tier = classify_tier(question_text)
    if min_tier is not None:
        tier = higher_tier(tier, min_tier)
    source_detail = dict(source_detail or {})
    if options:
        source_detail.setdefault("options_hash", options_hash(options))
    if applies_everywhere:
        source_detail["applies_everywhere"] = True

    with transaction.atomic():
        existing = _locked_row(profile, key, scope_region)
        if existing is None:
            try:
                # Savepoint: a concurrent first writer can win the unique
                # constraint between the read above and this insert.
                with transaction.atomic():
                    row = AnswerBank.objects.create(
                        profile=profile,
                        question_key=key,
                        scope_region=scope_region,
                        question_text=question_text or "",
                        value=value,
                        category=category or AnswerBank.Category.OTHER,
                        risk_tier=tier,
                        source=source,
                        source_detail=source_detail,
                        confidence=confidence,
                        is_locked=is_locked,
                    )
                return WriteResult(True, row)
            except IntegrityError:
                existing = _locked_row(profile, key, scope_region)
                if existing is None:
                    raise

        if source != AnswerBank.Source.USER and not _is_expired(existing, now):
            if SOURCE_RANK[source] < _rank(existing) or existing.is_locked:
                return WriteResult(False, existing)

        AnswerBankHistory.objects.create(
            profile=profile,
            question_key=key,
            scope_region=scope_region,
            value=existing.value,
            risk_tier=existing.risk_tier,
            source=existing.source,
            source_detail=existing.source_detail,
            confidence=existing.confidence,
            was_locked=existing.is_locked,
            superseded_by_source=source,
        )
        existing.question_text = question_text or existing.question_text
        existing.value = value
        existing.category = category or existing.category
        existing.risk_tier = higher_tier(existing.risk_tier, tier)
        existing.source = source
        existing.source_detail = source_detail
        existing.confidence = confidence
        existing.is_locked = is_locked
        existing.expires_at = None
        existing.save()
        return WriteResult(True, existing)
