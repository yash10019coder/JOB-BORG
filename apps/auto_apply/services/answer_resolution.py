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
    ResolutionReason,
    ResolvedAnswer,
    resolve_answers,
)
from apps.accounts.question_semantics import AuthKind, authorization_kind
from apps.accounts.services.answer_resolver import load_bank_rows, resolve_answer
from apps.auto_apply.llm.categories import QuestionCategory, classify
from apps.auto_apply.models import ExplicitAnswer

# A reason string for answers sourced from ExplicitAnswer -- not part of
# `llm.base.ResolutionReason`, which only enumerates LLM-orchestration
# outcomes; kept here so callers/tests can reference it by name.
EXPLICIT_ANSWER_REASON = "explicit_answer"

# An answer sourced from a `Profile` field collected for a different purpose
# (matching, not this exact question's wording) -- see `_profile_derived_answer`.
# Unlike EXPLICIT_ANSWER_REASON, always marked `needs_review=True`: the value
# is real and usable but was never confirmed by the user as *this* question's
# answer, so it surfaces in the review queue rather than being sent silently.
PROFILE_DERIVED_REASON = "profile_derived"
ANSWER_BANK_REASON = "answer_bank"

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
    ),
    QuestionCategory.SALARY_EXPECTATION: (ExplicitAnswer.Category.SALARY_EXPECTATION,),
}


def _explicit_categories_for(question_text: str, category: str) -> tuple[str, ...]:
    """Which `ExplicitAnswer` categories may answer this question.

    Both "Are you authorized to work?" and "Will you require visa
    sponsorship?" classify as WORK_AUTHORIZATION, but their answers are not
    interchangeable -- "No" to one means the opposite of "No" to the other, so
    a saved answer for the wrong sub-category must never be used. Split them by
    question wording; ambiguous status questions and combined questions
    require human review.
    """
    if category == QuestionCategory.WORK_AUTHORIZATION:
        kind = authorization_kind(question_text)
        if kind == AuthKind.SPONSORSHIP:
            return (ExplicitAnswer.Category.SPONSORSHIP,)
        if kind == AuthKind.AUTHORIZATION:
            return (ExplicitAnswer.Category.WORK_AUTHORIZATION,)
        return ()
    return _CATEGORY_TO_EXPLICIT_ANSWER_CATEGORIES.get(category, ())


def _profile_derived_answer(category: str, profile) -> str | None:
    """A direct value derivable from `Profile` data for a hard-excluded
    category with no `ExplicitAnswer` saved -- currently just salary
    expectation from `Profile.min_salary` (collected at signup for
    matching, per `apps.matching.scoring`, but otherwise unused here).

    Returns `None` when nothing on the profile answers this category, so
    the question falls through to its normal blank/needs_review handling
    exactly as before this existed.
    """
    if category == QuestionCategory.SALARY_EXPECTATION:
        min_salary = getattr(profile, "min_salary", None)
        if min_salary:
            return str(min_salary)
    return None


def resolve_field_answers(
    user,
    questions: list[Question],
    resume_text: str,
    profile,
    llm_client: AnswerInferenceClient,
) -> list[ResolvedAnswer]:
    """Resolve every question, preferring a saved `ExplicitAnswer`, then a
    directly-derivable `Profile` field, over LLM inference.

    For each question: classify it. If the classified category maps onto an
    `ExplicitAnswer.Category` the user has a saved answer for, use it
    directly (`needs_review=False`; the LLM client is never called for that
    question). Otherwise, if `_profile_derived_answer` can answer it from
    `Profile` data collected for another purpose (e.g. salary expectation
    from `Profile.min_salary`), use that instead (`needs_review=True`, since
    the value was never confirmed as *this* question's answer). Every
    remaining question is handed to `llm.base.resolve_answers()` as a single
    batch.

    Returns one `ResolvedAnswer` per input `question`, in the same order.
    """
    if not questions:
        return []

    explicit_by_category = {
        answer.category: answer for answer in ExplicitAnswer.objects.filter(user=user)
    }

    def legacy_lookup(question_text):
        """The old ExplicitAnswer table, handed to the resolver as its last
        resort so `apps.accounts` never imports `apps.auto_apply`."""
        category = classify(question_text)
        for candidate_category in _explicit_categories_for(question_text, category):
            if candidate_category in explicit_by_category:
                return explicit_by_category[candidate_category].answer_text
        return None

    resolved: dict[str, ResolvedAnswer] = {}
    remaining: list[Question] = []
    # One query for the whole draft instead of one per question.
    bank_rows = load_bank_rows(profile)

    for question in questions:
        category = classify(question.text)
        found = resolve_answer(
            profile,
            question.text,
            options=question.options,
            legacy_lookup=legacy_lookup,
            bank_rows=bank_rows,
        )

        if found is not None and found.value is None:
            # A stored answer exists but does not fit this form's options:
            # leave it blank for review rather than guessing a choice, and do
            # not let the LLM answer a question the user has already addressed.
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=None,
                needs_review=True,
                reason=ResolutionReason.INVALID_OPTION,
            )
            continue

        if found is not None:
            from_bank = found.provenance.get("origin") == "answer_bank"
            resolved[question.id] = _enforce_option_constraint(
                ResolvedAnswer(
                    question_id=question.id,
                    category=category,
                    answer=found.value,
                    # Legacy ExplicitAnswers are user-authored: confirmed,
                    # exactly as before. An unconfirmed T0/T1 bank value must
                    # be reviewed before it can be sent.
                    needs_review=found.needs_confirmation,
                    reason=ANSWER_BANK_REASON if from_bank else EXPLICIT_ANSWER_REASON,
                    needs_confirmation=found.needs_confirmation,
                    provenance=found.provenance if from_bank else None,
                    tier=found.tier if from_bank else "",
                ),
                question,
            )
            continue

        profile_answer = _profile_derived_answer(category, profile)
        if profile_answer is not None:
            resolved[question.id] = _enforce_option_constraint(
                ResolvedAnswer(
                    question_id=question.id,
                    category=category,
                    answer=profile_answer,
                    needs_review=True,
                    reason=PROFILE_DERIVED_REASON,
                ),
                question,
            )
        else:
            remaining.append(question)

    if remaining:
        # Single batched call for every question with no explicit-answer
        # override, sharing one resume/profile context -- resolve_answers()
        # itself enforces the one-call-per-batch contract.
        questions_by_id = {question.id: question for question in remaining}
        for answer in resolve_answers(remaining, resume_text, profile, llm_client):
            resolved[answer.question_id] = _enforce_option_constraint(
                answer, questions_by_id[answer.question_id]
            )

    return [resolved[q.id] for q in questions]


def _enforce_option_constraint(answer: ResolvedAnswer, question: Question) -> ResolvedAnswer:
    """Deterministic backstop for option-bearing questions (`SINGLE_SELECT`,
    `MULTI_SELECT`, `CHECKBOX_GROUP`): an LLM answer is trusted only when
    every selected value is an exact match against `question.options`.

    LLM instruction-following (the system prompt in `langchain_client.py`)
    is the first line of defense, not the enforcement boundary -- a model
    can still ignore the instruction and invent a value outside the option
    set. When that happens, the answer is treated exactly like any other
    unanswerable question (empty, `needs_review=True`) rather than letting
    an invalid value reach `answers_payload` and fail later at submit time
    (or silently misrepresent the applicant). Free-text questions
    (`question.options` empty) are untouched. A list-shaped answer (multiple
    simultaneous selections for MULTI_SELECT/CHECKBOX_GROUP) is valid only
    when every item in the list is a real option.

    Skipped entirely when `question.options_enforced` is `False` (an
    incomplete COMBOBOX_SELECT sample, per `Question`'s docstring) -- those
    `options` are shown to the LLM as a prompt hint only, not a closed set
    safe to reject an out-of-sample answer against.
    """
    if not question.options or not answer.answer or not question.options_enforced:
        return answer
    if isinstance(answer.answer, (list, tuple)):
        if all(value in question.options for value in answer.answer):
            return answer
    elif answer.answer in question.options:
        return answer
    return ResolvedAnswer(
        question_id=answer.question_id,
        category=answer.category,
        answer=None,
        needs_review=True,
        reason=ResolutionReason.INVALID_OPTION,
    )
