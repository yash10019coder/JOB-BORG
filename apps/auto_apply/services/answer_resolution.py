"""Caller-side answer resolution layered in front of U4's LLM-based
`resolve_answers`.

`apps.auto_apply.llm.base.resolve_answers()` deliberately consults no stored
answers: callers layer the user's own answers in front of LLM inference and
filter their question list before calling it. This module is that layer (U6,
per R5/R9, extended for the Profile Overhaul): every question goes through
`apps.accounts.services.answer_resolver.resolve_answer` -- typed profile
facts, then saved AnswerBank answers (including the backfilled legacy
`ExplicitAnswer` rows) -- and only what that cannot answer is handed to
`resolve_answers()`, preserving its category hard-exclusion / groundedness /
confidence gating and its one-call-per-batch behavior for the remainder.
"""
from __future__ import annotations

from apps.auto_apply.llm.base import (
    AnswerInferenceClient,
    Question,
    ResolutionReason,
    ResolvedAnswer,
    resolve_answers,
)
from apps.accounts.question_semantics import blocks_llm
from apps.accounts.services.answer_resolver import load_bank_rows, resolve_answer
from apps.auto_apply.greenhouse_form.field_mapping import TEXTAREA
from apps.auto_apply.llm.categories import classify

# A reason string for answers sourced from ExplicitAnswer -- not part of
# `llm.base.ResolutionReason`, which only enumerates LLM-orchestration
# outcomes; kept here so callers/tests can reference it by name.
EXPLICIT_ANSWER_REASON = "explicit_answer"

# An answer taken straight from the user's own profile settings (work
# authorization per country, citizenship, salary by region, contact facts).
PROFILE_FACT_REASON = "profile_fact"
# A question the typed layer owns (citizenship, "are you located in X") that
# the profile could not answer confidently: left blank for review, never
# guessed by the LLM.
TYPED_FACT_UNANSWERED_REASON = "typed_fact_unanswered"
ANSWER_BANK_REASON = "answer_bank"

def resolve_field_answers(
    user,
    questions: list[Question],
    resume_text: str,
    profile,
    llm_client: AnswerInferenceClient,
    job=None,
) -> list[ResolvedAnswer]:
    """Resolve every question from what the user has already told us, and only
    then ask the LLM.

    Each question goes through `resolve_answer`: the user's typed profile
    facts first, then their saved answers, then the backfilled legacy
    answers. A question the typed layer owns but the profile cannot answer
    confidently (a citizenship or "are you located in X" question) is left
    blank for review and never reaches the LLM. Every remaining question is
    handed to `llm.base.resolve_answers()` as a single batch.

    `job` supplies the country (for per-country authorization and salary
    bands), the region (for location-sensitive saved answers) and the
    employer name (stripped from the question key for short factual fields,
    kept for narrative `textarea` questions).

    Returns one `ResolvedAnswer` per input `question`, in the same order.
    """
    if not questions:
        return []

    resolved: dict[str, ResolvedAnswer] = {}
    remaining: list[Question] = []
    # One query for the whole draft instead of one per question.
    bank_rows = load_bank_rows(profile)

    for question in questions:
        category = classify(question.text)
        found = resolve_answer(
            profile,
            question.text,
            job,
            options=question.options,
            bank_rows=bank_rows,
            strip_employer=question.field_type != TEXTAREA,
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
            origin = found.provenance.get("origin", "")
            # Legacy answers keep their original shape (no provenance block);
            # bank rows and profile facts carry theirs into the draft.
            has_provenance = origin != "legacy_explicit_answer"
            if origin == "answer_bank":
                reason = ANSWER_BANK_REASON
            elif has_provenance:
                reason = PROFILE_FACT_REASON
            else:
                reason = EXPLICIT_ANSWER_REASON
            resolved[question.id] = _enforce_option_constraint(
                ResolvedAnswer(
                    question_id=question.id,
                    category=category,
                    answer=found.value,
                    # Legacy ExplicitAnswers are user-authored: confirmed,
                    # exactly as before. An unconfirmed T0/T1 bank value must
                    # be reviewed before it can be sent.
                    needs_review=found.needs_confirmation,
                    reason=reason,
                    needs_confirmation=found.needs_confirmation,
                    provenance=found.provenance if has_provenance else None,
                    tier=found.tier if has_provenance else "",
                ),
                question,
            )
            continue

        if blocks_llm(question.text):
            resolved[question.id] = ResolvedAnswer(
                question_id=question.id,
                category=category,
                answer=None,
                needs_review=True,
                reason=TYPED_FACT_UNANSWERED_REASON,
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
