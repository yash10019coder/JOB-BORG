"""Vendor-agnostic answer-inference interface, provider registry, and the
deterministic answer-resolution gate.

``AnswerInferenceClient`` is a ``Protocol`` any LLM vendor implementation
satisfies. ``resolve_answers`` is the orchestration-facing entry point (used
by U6's drafting service): it applies the category hard-exclusion boundary
(``categories.HARD_EXCLUDED_CATEGORIES``), batches every remaining allowed
question into a *single* ``infer()`` call sharing one resume/profile
context, then runs the deterministic evidence-groundedness check before
``self_reported_confidence`` is even consulted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings

from .categories import HARD_EXCLUDED_CATEGORIES, classify

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Question:
    """A single rendered application question.

    Deliberately decoupled from the ``apps.auto_apply`` ORM models (built by
    a separate implementation unit) so this module has no hard dependency on
    them -- callers adapt their own question representation into this shape.
    ``text`` is untrusted input: it is the employer's Greenhouse form label,
    not something JobBorg authored (see ``langchain_client``'s prompt
    handling and the groundedness check below).
    """

    id: str
    text: str


@dataclass(frozen=True)
class QuestionAnswer:
    """One LLM-inferred answer, as returned by ``AnswerInferenceClient.infer``."""

    question_id: str
    answer: str
    evidence: list[str] = field(default_factory=list)
    self_reported_confidence: float = 0.0
    insufficient_evidence: bool = False


@dataclass(frozen=True)
class ResolvedAnswer:
    """The outcome of running a ``Question`` through ``resolve_answers``."""

    question_id: str
    category: str
    answer: str | None
    needs_review: bool
    reason: str


# Reasons a ResolvedAnswer can carry -- kept as plain strings (not an enum)
# to match the rest of the codebase's lightweight style, but centralized
# here so callers/tests can reference them by name instead of literals.
class ResolutionReason:
    HARD_EXCLUDED_CATEGORY = "hard_excluded_category"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNGROUNDED_EVIDENCE = "ungrounded_evidence"
    LOW_CONFIDENCE = "low_confidence"
    LLM_CALL_FAILED = "llm_call_failed"
    MISSING_LLM_RESPONSE = "missing_llm_response"
    OK = "ok"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class AnswerInferenceClient(Protocol):
    """Vendor-agnostic interface every LLM answer-inference provider implements."""

    def infer(
        self, questions: list[Question], resume_text: str, profile
    ) -> list[QuestionAnswer]:
        """Infer answers for every question in a single batched call.

        Implementations MUST NOT make one round trip per question -- callers
        (``resolve_answers`` below) rely on batching all allowed-category
        questions for one application into a single call sharing one
        resume/profile context.
        """
        ...


def get_client(provider: str | None = None, **kwargs) -> AnswerInferenceClient:
    """Return a new client instance for ``provider`` (defaults to settings).

    Every provider is driven by the single ``LangChainAnswerInferenceClient``
    (see ``langchain_client.py``), configured per-provider through the
    data-only ``_PROVIDER_CONFIGS`` table -- not a registry of classes.
    Imported lazily here (not at module level) because ``langchain_client``
    imports ``Question``/``QuestionAnswer``/``_profile_text`` from this
    module, and those names aren't defined yet at the point a top-level
    import of ``langchain_client`` would run -- avoids a module-import-order-
    dependent circular import between the two files.

    Raises:
        ValueError: ``provider`` is not a registered provider.
    """
    from .langchain_client import _PROVIDER_CONFIGS, LangChainAnswerInferenceClient

    provider = provider or settings.AUTO_APPLY_LLM_PROVIDER
    try:
        provider_config = _PROVIDER_CONFIGS[provider]
    except KeyError:
        raise ValueError(
            f"No AnswerInferenceClient registered for provider={provider!r}. "
            f"Registered: {sorted(_PROVIDER_CONFIGS)}"
        ) from None
    return LangChainAnswerInferenceClient(provider_config, **kwargs)


# ---------------------------------------------------------------------------
# Groundedness check
# ---------------------------------------------------------------------------
# Profile fields that plausibly hold free text worth checking evidence
# against, beyond resume_text. Looked up defensively via getattr so this
# module has no hard dependency on the exact Profile schema (owned by a
# separate implementation unit) -- unknown/missing attributes are skipped.
_PROFILE_TEXT_FIELDS = (
    "full_name",
    "headline",
    "current_title",
    "summary",
    "bio",
    "target_titles",
    "target_tags",
)


def _profile_text(profile) -> str:
    if profile is None:
        return ""
    parts = []
    for field_name in _PROFILE_TEXT_FIELDS:
        value = getattr(profile, field_name, None)
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            parts.append(" ".join(str(item) for item in value))
        else:
            parts.append(str(value))
    return "\n".join(parts)


def evidence_appears_in(evidence: list[str], resume_text: str, profile) -> bool:
    """Deterministic check: does every cited evidence span actually appear
    in the supplied resume/profile text?

    This is the primary defense against a manipulated or hallucinated
    answer surviving into a draft -- it runs before self-reported confidence
    is ever consulted (see ``resolve_answers``). An empty evidence list is
    never considered grounded: an answer with no cited support is exactly
    the case this check exists to catch.
    """
    if not evidence:
        return False
    haystack = f"{resume_text or ''}\n{_profile_text(profile)}".lower()
    for span in evidence:
        if not span or not span.strip():
            return False
        if span.strip().lower() not in haystack:
            return False
    return True


# ---------------------------------------------------------------------------
# Answer resolution
# ---------------------------------------------------------------------------
def resolve_answers(
    questions: list[Question],
    resume_text: str,
    profile,
    llm_client: AnswerInferenceClient,
) -> list[ResolvedAnswer]:
    """Resolve every question to a ``ResolvedAnswer``, applying the category
    hard-exclusion boundary, a single batched LLM call for the remainder,
    and the deterministic groundedness/confidence gate.

    Note: this does not consult ``ExplicitAnswer`` records (owned by a
    separate implementation unit) -- callers that want the explicit-answer
    override layered in front of LLM inference filter their question list
    accordingly before calling this. It also does not attempt to preserve
    the original request order of `questions` beyond mapping every input
    question to exactly one output ``ResolvedAnswer``.
    """
    resolved: dict[str, ResolvedAnswer] = {}
    allowed: list[tuple[Question, str]] = []

    for question in questions:
        category = classify(question.text)
        if category in HARD_EXCLUDED_CATEGORIES:
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=None,
                needs_review=True,
                reason=ResolutionReason.HARD_EXCLUDED_CATEGORY,
            )
        else:
            allowed.append((question, category))

    if not allowed:
        return [resolved[q.id] for q in questions]

    try:
        # Single batched call sharing one resume/profile context -- never
        # one call per question (see the Protocol docstring and the plan's
        # batch-per-draft rationale).
        answers = llm_client.infer([q for q, _ in allowed], resume_text, profile)
    except Exception:
        logger.exception(
            "AnswerInferenceClient.infer failed for batch of %d question(s); "
            "routing all to needs_review",
            len(allowed),
        )
        for question, category in allowed:
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=None,
                needs_review=True,
                reason=ResolutionReason.LLM_CALL_FAILED,
            )
        return [resolved[q.id] for q in questions]

    answers_by_id = {qa.question_id: qa for qa in answers}
    confidence_threshold = settings.AUTO_APPLY_CONFIDENCE_THRESHOLD

    for question, category in allowed:
        qa = answers_by_id.get(question.id)
        if qa is None:
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=None,
                needs_review=True,
                reason=ResolutionReason.MISSING_LLM_RESPONSE,
            )
            continue

        if qa.insufficient_evidence:
            reason = ResolutionReason.INSUFFICIENT_EVIDENCE
            needs_review = True
        elif not evidence_appears_in(qa.evidence, resume_text, profile):
            # Groundedness check runs first, and overrides self-reported
            # confidence unconditionally -- a confidently-stated but
            # ungrounded answer is still forced to needs_review.
            reason = ResolutionReason.UNGROUNDED_EVIDENCE
            needs_review = True
        elif qa.self_reported_confidence < confidence_threshold:
            # Confidence is only a secondary tiebreaker, consulted after the
            # groundedness check has already passed.
            reason = ResolutionReason.LOW_CONFIDENCE
            needs_review = True
        else:
            reason = ResolutionReason.OK
            needs_review = False

        resolved[question.id] = ResolvedAnswer(
            question_id=question.id,
            category=category,
            answer=qa.answer,
            needs_review=needs_review,
            reason=reason,
        )

    return [resolved[q.id] for q in questions]
