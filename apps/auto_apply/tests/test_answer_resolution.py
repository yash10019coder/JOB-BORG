"""Tests for `apps.auto_apply.services.answer_resolution` (U3): the
deterministic option-constraint backstop layered around `resolve_answers`.

`FakeLLMClient` stands in for `AnswerInferenceClient` -- no real LLM call is
ever made.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.auto_apply.llm.base import Question, QuestionAnswer, ResolutionReason
from apps.auto_apply.services.answer_resolution import resolve_field_answers

User = get_user_model()


class FakeLLMClient:
    def __init__(self, answers_by_id=None):
        self.answers_by_id = answers_by_id or {}
        self.calls = []

    def infer(self, questions, resume_text, profile):
        self.calls.append((questions, resume_text, profile))
        return [self.answers_by_id[q.id] for q in questions if q.id in self.answers_by_id]


class OptionConstraintEnforcementTests(TestCase):
    """U3: an LLM answer for an option-bearing question is only trusted when
    it exactly matches one of `question.options`."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="bob", password="pw", email="bob@example.com"
        )
        self.profile = self.user.profile
        self.profile.resume_text = "Bob Jones. Graduated from State University."
        self.profile.save()

    def _resolve(self, question, answer):
        llm_client = FakeLLMClient(answers_by_id={question.id: answer})
        return resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, llm_client
        )[0]

    def test_exact_option_match_passes_through_unchanged(self):
        question = Question(
            id="What is your highest level of education?",
            text="What is your highest level of education?",
            field_type="single_select",
            options=("High School", "Bachelor's", "Other"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            answer="Bachelor's",
            evidence=["State University"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "Bachelor's")
        self.assertFalse(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.OK)

    def test_answer_not_in_options_is_treated_as_unanswerable(self):
        question = Question(
            id="What college did you attend?",
            text="What college did you attend?",
            field_type="single_select",
            options=("State University A", "State University B", "Other"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            # The applicant's real college, invented/free-text -- not one of
            # the form's listed options.
            answer="Community College of Somewhere Else",
            evidence=["Graduated from State University"],
            self_reported_confidence=0.95,
        )

        resolved = self._resolve(question, answer)

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INVALID_OPTION)

    def test_empty_answer_for_option_bearing_question_is_unaffected(self):
        question = Question(
            id="q1",
            text="What is your highest level of education?",
            field_type="single_select",
            options=("High School", "Bachelor's"),
        )
        answer = QuestionAnswer(
            question_id="q1", answer="", evidence=[], self_reported_confidence=0.0,
            insufficient_evidence=True,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "")
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INSUFFICIENT_EVIDENCE)

    def test_free_text_question_is_never_option_validated(self):
        question = Question(
            id="q1", text="Tell us about your experience", field_type="text"
        )
        answer = QuestionAnswer(
            question_id="q1",
            answer="Five years of backend engineering.",
            evidence=["Graduated from State University"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "Five years of backend engineering.")
        self.assertFalse(resolved.needs_review)

    def test_case_sensitive_mismatch_is_rejected(self):
        question = Question(
            id="q1",
            text="Are you willing to relocate?",
            field_type="single_select",
            options=("Yes", "No"),
        )
        answer = QuestionAnswer(
            question_id="q1",
            answer="yes",
            evidence=["Graduated from State University"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INVALID_OPTION)
