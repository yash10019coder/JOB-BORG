"""Earlier answered questions that look like a reworded one (Phase 4b, #130).

``find_candidates`` returns up to a few of the asking profile's own saved
answers whose *question* resembles the one being asked. It is read-only and
propose-only: a candidate is shown to the reviewer next to a blank field and is
never a value, never confirms anything and never touches ``AnswerBank``. The
matching itself (trigram similarity, intents, the vetoes for opposite meanings,
countries, tiers and the strict word-set rule for legal and commercial
questions) lives in the pure leaf :mod:`apps.accounts.question_matching`; this
module only decides *which rows are eligible* and how to present them:

* only the asking profile's rows, whatever the caller passed in;
* not expired, not the ``legacy:*`` backfill, not a row with no question text;
* never the exact key (that is ``resolve_answer``'s case, not a candidate);
* learned rows only while the profile has learning on;
* a location-sensitive question (relocation, on-site...) only from a row saved
  for the job's own region or marked "applies everywhere", the same rule the
  resolver applies, so an answer given for a US job is never offered on an
  India job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.accounts import question_matching as qm
from apps.accounts import question_semantics as qs
from apps.accounts.models import AnswerBank
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import (
    _employer_name,
    _is_expired,
    _map_to_options,
    normalize_question_key,
)
from apps.accounts.services.precedence import LOCKED_RANK, SOURCE_RANK

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    """One earlier answered question offered for a reworded one.

    ``mapped_value`` is the stored value put onto the live field's options, or
    ``None`` when it does not fit this form (the candidate is then shown as
    text only and cannot be accepted).
    """

    question_key: str
    scope_region: str
    question_text: str
    value: Any
    mapped_value: Any
    score: float
    via: str
    intent: str | None
    source: str
    tier: str
    is_locked: bool


def _enabled() -> bool:
    return bool(getattr(settings, "QUESTION_CANDIDATES_ENABLED", False))


def _rank(row):
    return LOCKED_RANK if row.is_locked else SOURCE_RANK[row.source]


def _region_ok(row, question_text, job_region):
    """The resolver's location rule, applied to candidate rows."""
    if not (qs.is_location_sensitive(question_text) or qs.is_location_sensitive(row.question_text)):
        return True
    if (row.source_detail or {}).get("applies_everywhere"):
        return True
    return bool(job_region) and row.scope_region == job_region


def _pool(profile, bank_rows):
    """The profile's own rows, even when the caller preloaded a mixed dict."""
    rows = bank_rows.values() if bank_rows is not None else AnswerBank.objects.filter(profile=profile)
    return [row for row in rows if row.profile_id == profile.pk]


def find_candidates(
    profile,
    question_text,
    job=None,
    *,
    options=(),
    bank_rows: dict | None = None,
    strip_employer: bool = True,
    limit: int | None = None,
    min_score: float | None = None,
    now=None,
) -> list[Candidate]:
    """Up to ``limit`` candidates for ``question_text``, best first.

    Returns ``[]`` when candidates are switched off, the profile is missing, or
    nothing qualifies. ``bank_rows`` is the optional :func:`load_bank_rows`
    preload (no per-question query when given). Never writes.
    """
    if profile is None or not _enabled() or not str(question_text or "").strip():
        return []
    limit = limit if limit is not None else getattr(settings, "QUESTION_CANDIDATE_MAX", 3)
    min_score = min_score if min_score is not None else getattr(
        settings, "QUESTION_CANDIDATE_MIN_SCORE", qm.DEFAULT_MIN_SCORE
    )
    now = now or timezone.now()
    options = tuple(options or ())
    own_key = normalize_question_key(question_text, _employer_name(job) if strip_employer else None)
    job_region = typed_facts.job_region(job)

    found = []
    for row in _pool(profile, bank_rows):
        if not (row.question_text or "").strip() or row.question_key.startswith("legacy:"):
            continue
        if row.question_key == own_key or _is_expired(row, now):
            continue
        if row.source == AnswerBank.Source.LEARNED and not profile.learning_enabled:
            continue
        if not _region_ok(row, question_text, job_region):
            continue
        result = qm.match(question_text, row.question_text, min_score=min_score)
        if not result.matched:
            continue
        found.append((row, result))

    found.sort(key=lambda pair: (-_rank(pair[0]), -pair[1].score, -pair[0].updated_at.timestamp()))
    return [
        Candidate(
            question_key=row.question_key,
            scope_region=row.scope_region,
            question_text=row.question_text,
            value=row.value,
            mapped_value=_map_to_options(row.value, options),
            score=round(result.score, 3),
            via=result.via,
            intent=result.intent,
            source=row.source,
            tier=row.risk_tier,
            is_locked=row.is_locked,
        )
        for row, result in found[: max(limit, 0)]
    ]
