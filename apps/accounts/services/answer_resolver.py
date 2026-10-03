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

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AnswerBank, AnswerBankHistory
from apps.accounts.tiering import Tier, classify_tier, higher_tier

logger = logging.getLogger(__name__)

# FR7.7: learned T2 answers expire after 180 days, imported after 365, unless
# re-confirmed (any write refreshes ``updated_at``). Assumptions pending real
# answer-change data (see Open Questions in the requirements doc).
LEARNED_T2_TTL = timedelta(days=180)
IMPORTED_T2_TTL = timedelta(days=365)

_MAX_KEY_LEN = 255
_NEEDS_CONFIRMATION_TIERS = {Tier.T0_LEGAL, Tier.T1_COMMERCIAL}

_SOURCE_RANK = {
    AnswerBank.Source.IMPORTED: 1,
    AnswerBank.Source.LEARNED: 2,
    AnswerBank.Source.USER: 3,
}
_LOCKED_RANK = 4


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


def normalize_question_key(question_text, options=()):
    """Stable key for a question: lowercased, punctuation and whitespace
    collapsed. For select/boolean fields the (sorted) option set is hashed in,
    so the same wording with different choices is a different question.
    """
    base = re.sub(r"[^\w]+", " ", (question_text or "").lower()).strip()
    suffix = ""
    if options:
        digest = hashlib.sha1("\x1f".join(sorted(map(str, options))).encode()).hexdigest()
        suffix = f"#{digest[:10]}"
    if len(base) + len(suffix) > _MAX_KEY_LEN:
        digest = hashlib.sha1(base.encode()).hexdigest()[:10]
        base = f"{base[: _MAX_KEY_LEN - len(suffix) - 11]}~{digest}"
    return base + suffix


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
    return _LOCKED_RANK if row.is_locked else _SOURCE_RANK[row.source]


def resolve_answer(
    profile,
    question_text,
    job=None,
    *,
    options=(),
    legacy_lookup: Callable[[str], Any] | None = None,
    now=None,
):
    """Return a :class:`ResolvedValue`, or ``None`` if nothing is known."""
    now = now or timezone.now()
    key = normalize_question_key(question_text, options)
    computed_tier = classify_tier(question_text)

    row = AnswerBank.objects.filter(profile=profile, question_key=key).first()
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
                extra={"profile_id": profile.pk, "question_key": key},
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


def write_answer(
    profile,
    question_text,
    value,
    source,
    *,
    options=(),
    category=AnswerBank.Category.OTHER,
    source_detail=None,
    confidence=1.0,
    is_locked=False,
    now=None,
):
    """Create or replace the ``AnswerBank`` row for a question.

    Returns ``WriteResult(applied=False, row=<existing>)`` when a higher-
    precedence row already holds the question -- the caller may then propose
    the value as a suggestion instead. A replaced value is kept in
    ``AnswerBankHistory`` in the same transaction.
    """
    source = AnswerBank.Source(source)
    if is_locked and source != AnswerBank.Source.USER:
        raise ValueError("only the user may lock an answer")
    key = normalize_question_key(question_text, options)
    tier = classify_tier(question_text)

    with transaction.atomic():
        existing = (
            AnswerBank.objects.select_for_update()
            .filter(profile=profile, question_key=key)
            .first()
        )
        if existing is None:
            row = AnswerBank.objects.create(
                profile=profile,
                question_key=key,
                question_text=question_text or "",
                value=value,
                category=category,
                risk_tier=tier,
                source=source,
                source_detail=source_detail or {},
                confidence=confidence,
                is_locked=is_locked,
            )
            return WriteResult(True, row)

        if source != AnswerBank.Source.USER and _SOURCE_RANK[source] < _rank(existing):
            return WriteResult(False, existing)
        if source != AnswerBank.Source.USER and existing.is_locked:
            return WriteResult(False, existing)

        AnswerBankHistory.objects.create(
            profile=profile,
            question_key=key,
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
        existing.category = category
        existing.risk_tier = higher_tier(existing.risk_tier, tier)
        existing.source = source
        existing.source_detail = source_detail or {}
        existing.confidence = confidence
        existing.is_locked = is_locked
        existing.expires_at = None
        existing.save()
        return WriteResult(True, existing)
