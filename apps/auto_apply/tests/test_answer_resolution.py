"""Tests for `apps.auto_apply.services.answer_resolution` (U3): the
deterministic option-constraint backstop layered around `resolve_answers`.

`FakeLLMClient` stands in for `AnswerInferenceClient` -- no real LLM call is
ever made.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.auto_apply.llm.base import Question, QuestionAnswer, ResolutionReason
from apps.auto_apply.models import ExplicitAnswer
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


class ExplicitAnswerOptionConstraintTests(TestCase):
    """Regression guard: a saved `ExplicitAnswer` must be validated against
    an option-bearing question's real options exactly like an LLM answer --
    it previously bypassed `_enforce_option_constraint` entirely, so a
    stored explicit answer for e.g. WORK_AUTHORIZATION could silently
    violate the field's real option set."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="carol", password="pw", email="carol@example.com"
        )
        self.profile = self.user.profile
        self.profile.resume_text = "Carol Lee."
        self.profile.save()

    def test_explicit_answer_matching_an_option_passes_through(self):
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.WORK_AUTHORIZATION,
            answer_text="Yes",
        )
        question = Question(
            id="Are you authorized to work in the US?",
            text="Are you authorized to work in the US?",
            field_type="single_select",
            options=("Yes", "No"),
        )

        resolved = resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, FakeLLMClient()
        )[0]

        self.assertEqual(resolved.answer, "Yes")
        self.assertFalse(resolved.needs_review)
        self.assertEqual(resolved.reason, "explicit_answer")

    def test_explicit_answer_not_in_options_is_treated_as_unanswerable(self):
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.WORK_AUTHORIZATION,
            answer_text="Yes, I am authorized to work without restriction.",
        )
        question = Question(
            id="Are you authorized to work in the US?",
            text="Are you authorized to work in the US?",
            field_type="single_select",
            options=("Yes", "No"),
        )

        resolved = resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, FakeLLMClient()
        )[0]

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INVALID_OPTION)


class MultiValueOptionFieldTests(TestCase):
    """MULTI_SELECT/CHECKBOX_GROUP questions are option-bearing too, but
    `QuestionAnswer.answer` is always a single string (a pre-existing shape,
    not introduced by this change) -- document that the same exact-match
    gate applies to these field types: a single-string answer that matches
    one real option passes, and anything else (including a would-be
    multi-value answer) is treated as unanswerable rather than silently
    submitted."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="dave", password="pw", email="dave@example.com"
        )
        self.profile = self.user.profile
        self.profile.resume_text = "Dave Kim. Proficient in Python and Go."
        self.profile.save()

    def _resolve(self, question, answer):
        llm_client = FakeLLMClient(answers_by_id={question.id: answer})
        return resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, llm_client
        )[0]

    def test_checkbox_group_single_matching_option_passes(self):
        question = Question(
            id="Which languages do you know?",
            text="Which languages do you know?",
            field_type="checkbox_group",
            options=("Python", "Go", "Rust"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            answer="Python",
            evidence=["Proficient in Python and Go"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "Python")
        self.assertFalse(resolved.needs_review)

    def test_multi_select_comma_joined_answer_is_treated_as_unanswerable(self):
        # The LLM has no way to express "both Python and Go" as a single
        # exact-match option string -- a comma-joined guess correctly fails
        # the exact-match gate rather than being submitted as a bogus value.
        question = Question(
            id="Which languages do you know?",
            text="Which languages do you know?",
            field_type="multi_select",
            options=("Python", "Go", "Rust"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            answer="Python, Go",
            evidence=["Proficient in Python and Go"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INVALID_OPTION)
