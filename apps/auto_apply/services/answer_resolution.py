"""Explicit-answer lookup layered in front of U4's LLM-based `resolve_answers`.

`apps.auto_apply.llm.base.resolve_answers()` deliberately does not consult
`ExplicitAnswer` -- its own docstring says as much: "this does not consult
`ExplicitAnswer` records (owned by a separate implementation unit) --
callers that want the explicit-answer override layered in front of LLM
inference filter their question list accordingly before calling this."

This module is that caller-side layer (U6, per R5/R9): for each rendered
question, a saved `ExplicitAnswer` -- if the question's classified category
maps onto one the user has -- always wins over LLM inference, and the LLM
client is never consulted for that question. Only questions with no
matching explicit answer are handed to `resolve_answers()`, preserving that
function's category hard-exclusion / groundedness / confidence gating and
its one-call-per-batch behavior for the remainder.
"""
from __future__ import annotations

from apps.auto_apply.llm.base import (
    AnswerInferenceClient,
    Question,
    ResolvedAnswer,
    resolve_answers,
)
from apps.auto_apply.llm.categories import QuestionCategory, classify
from apps.auto_apply.models import ExplicitAnswer

# A reason string for answers sourced from ExplicitAnswer -- not part of
# `llm.base.ResolutionReason`, which only enumerates LLM-orchestration
# outcomes; kept here so callers/tests can reference it by name.
EXPLICIT_ANSWER_REASON = "explicit_answer"

# Which ExplicitAnswer.Category values satisfy a question classified into a
# given QuestionCategory (categories.py). Only categories with a real
# analog in ExplicitAnswer.Category are mapped here -- QuestionCategory.
# GENERIC and the hard-excluded categories with no ExplicitAnswer analog
# (LEGAL_ATTESTATION, BACKGROUND_CHECK) simply fall through to
# resolve_answers(), which already routes them to needs_review the same way
# it always has (hard-exclusion for the latter two, LLM inference for the
# former).
_CATEGORY_TO_EXPLICIT_ANSWER_CATEGORIES: dict[str, tuple[str, ...]] = {
    QuestionCategory.WORK_AUTHORIZATION: (
        ExplicitAnswer.Category.WORK_AUTHORIZATION,
        ExplicitAnswer.Category.SPONSORSHIP,
    ),
    QuestionCategory.SALARY_EXPECTATION: (ExplicitAnswer.Category.SALARY_EXPECTATION,),
}


def resolve_field_answers(
    user,
    questions: list[Question],
    resume_text: str,
    profile,
    llm_client: AnswerInferenceClient,
) -> list[ResolvedAnswer]:
    """Resolve every question, preferring a saved `ExplicitAnswer` over LLM
    inference.

    For each question: classify it, and if the classified category maps
    onto an `ExplicitAnswer.Category` the user has a saved answer for, use
    it directly (`needs_review=False`; the LLM client is never called for
    that question). Every remaining question is handed to
    `llm.base.resolve_answers()` as a single batch.

    Returns one `ResolvedAnswer` per input `question`, in the same order.
    """
    if not questions:
        return []

    explicit_by_category = {
        answer.category: answer for answer in ExplicitAnswer.objects.filter(user=user)
    }

    resolved: dict[str, ResolvedAnswer] = {}
    remaining: list[Question] = []

    for question in questions:
        category = classify(question.text)
        explicit = None
        for candidate_category in _CATEGORY_TO_EXPLICIT_ANSWER_CATEGORIES.get(category, ()):
            if candidate_category in explicit_by_category:
                explicit = explicit_by_category[candidate_category]
                break

        if explicit is not None:
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=explicit.answer_text,
                needs_review=False,
                reason=EXPLICIT_ANSWER_REASON,
            )
        else:
            remaining.append(question)

    if remaining:
        # Single batched call for every question with no explicit-answer
        # override, sharing one resume/profile context -- resolve_answers()
        # itself enforces the one-call-per-batch contract.
        for answer in resolve_answers(remaining, resume_text, profile, llm_client):
            resolved[answer.question_id] = answer

    return [resolved[q.id] for q in questions]
