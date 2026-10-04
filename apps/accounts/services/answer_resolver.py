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

from apps.accounts.models import AnswerBank, AnswerBankHistory
from apps.accounts.services.precedence import LOCKED_RANK, SOURCE_RANK
from apps.accounts.tiering import Tier, classify_tier, higher_tier

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


def resolve_answer(
    profile,
    question_text,
    job=None,
    *,
    options=(),
    legacy_lookup: Callable[[str], Any] | None = None,
    bank_rows: dict | None = None,
    now=None,
):
    """Return a :class:`ResolvedValue`, or ``None`` if nothing is known.

    ``bank_rows`` is an optional :func:`load_bank_rows` preload; when given, no
    per-question query is made.
    """
    now = now or timezone.now()
    key = normalize_question_key(question_text, _employer_name(job))
    computed_tier = classify_tier(question_text)

    if bank_rows is not None:
        row = bank_rows.get((key, ""))
    elif profile is not None:
        row = AnswerBank.objects.filter(
            profile=profile, question_key=key, scope_region=""
        ).first()
    else:
        row = None
    if row is not None and not _is_expired(row, now):
        tier = higher_tier(row.risk_tier, computed_tier)
        return ResolvedValue(
            value=row.value,
            provenance={
                "origin": "answer_bank",
                "source": row.source,
                "locked": row.is_locked,
                "confidence": row.confidence,
                "detail": row.source_detail,
            },
            needs_confirmation=(
                tier in _NEEDS_CONFIRMATION_TIERS and row.source != AnswerBank.Source.USER
            ),
            tier=tier,
        )

    if legacy_lookup is not None:
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
