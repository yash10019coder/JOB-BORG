"""Tests for `apps.auto_apply.services.answer_resolution` (U3): the
deterministic option-constraint backstop layered around `resolve_answers`.

`FakeLLMClient` stands in for `AnswerInferenceClient` -- no real LLM call is
ever made.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.auto_apply.llm.base import Question, QuestionAnswer, ResolutionReason
from apps.auto_apply.models import ExplicitAnswer
from apps.auto_apply.services.answer_resolution import (
    PROFILE_DERIVED_REASON, resolve_field_answers,
)

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
        self.profile.resume_text = (
            "Bob Jones. Graduated from State University with a Bachelor's degree. "
            "Five years of backend engineering experience. "
            "Proficient in Python and Go."
        )
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
            evidence=["Bachelor's degree"],
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

    def test_multi_select_answer_with_all_valid_options_passes_through(self):
        question = Question(
            id="Which languages do you know?",
            text="Which languages do you know?",
            field_type="multi_select",
            options=("Python", "Go", "Rust"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            answer=["Python", "Go"],
            evidence=["Proficient in Python and Go"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, ["Python", "Go"])
        self.assertFalse(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.OK)

    def test_multi_select_answer_with_one_invalid_option_is_rejected(self):
        question = Question(
            id="Which languages do you know?",
            text="Which languages do you know?",
            field_type="multi_select",
            options=("Python", "Go", "Rust"),
        )
        answer = QuestionAnswer(
            question_id=question.id,
            answer=["Python", "COBOL"],
            evidence=["Proficient in Python and Go"],
            self_reported_confidence=0.9,
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
            evidence=["Five years of backend engineering experience"],
            self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "Five years of backend engineering.")
        self.assertFalse(resolved.needs_review)

    def test_options_enforced_false_lets_an_out_of_sample_answer_through(self):
        # An incomplete COMBOBOX_SELECT sample (options_enforced=False, per
        # drafting.py) is a hint for the LLM, not a closed set -- a real
        # answer the sample simply didn't happen to include must not be
        # rejected the way test_answer_not_in_options_is_treated_as_
        # unanswerable's fully-enforced case is.
        self.profile.resume_text += " Holds a Master's degree in Computer Science."
        self.profile.save()
        question = Question(
            id="q1", text="What is your highest level of education?",
            field_type="combobox_select", options=("High School", "Bachelor's"),
            options_enforced=False,
        )
        answer = QuestionAnswer(
            question_id="q1", answer="Master's",
            evidence=["Master's degree"], self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertEqual(resolved.answer, "Master's")
        self.assertFalse(resolved.needs_review)

    def test_options_enforced_true_still_rejects_out_of_sample_by_default(self):
        # Default (options_enforced=True, unset) preserves the existing
        # exact-match gate -- this is the same scenario as
        # test_answer_not_in_options_is_treated_as_unanswerable, confirming
        # the new field doesn't weaken enforcement when not explicitly opted
        # out of.
        question = Question(
            id="q1", text="What is your highest level of education?",
            field_type="single_select", options=("High School", "Bachelor's"),
        )
        answer = QuestionAnswer(
            question_id="q1", answer="Master's",
            evidence=["Master's degree"], self_reported_confidence=0.9,
        )

        resolved = self._resolve(question, answer)

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)

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


class WorkAuthorizationSponsorshipSplitTests(TestCase):
    """"Are you authorized to work?" and "Will you require sponsorship?" both
    classify as WORK_AUTHORIZATION but are opposite in meaning; a saved answer
    for one must never answer the other."""

    AUTH_Q = "Are you legally authorized to work in the United States?"
    SPONSOR_Q = "Will you now or in the future require visa sponsorship?"

    def setUp(self):
        self.user = User.objects.create_user(
            username="frank", password="pw", email="frank@example.com"
        )
        self.profile = self.user.profile
        self.profile.resume_text = "Frank."
        self.profile.save()

    def _resolve(self, question_text):
        question = Question(id=question_text, text=question_text, field_type="text", options=None)
        return resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, FakeLLMClient()
        )[0]

    def test_each_question_gets_its_own_saved_answer(self):
        ExplicitAnswer.objects.create(
            user=self.user, category=ExplicitAnswer.Category.WORK_AUTHORIZATION, answer_text="US Citizen"
        )
        ExplicitAnswer.objects.create(
            user=self.user, category=ExplicitAnswer.Category.SPONSORSHIP, answer_text="No"
        )

        self.assertEqual(self._resolve(self.AUTH_Q).answer, "US Citizen")
        self.assertEqual(self._resolve(self.SPONSOR_Q).answer, "No")

    def test_saved_sponsorship_answer_is_not_used_for_the_authorization_question(self):
        ExplicitAnswer.objects.create(
            user=self.user, category=ExplicitAnswer.Category.SPONSORSHIP,
            answer_text="No, I do not require sponsorship.",
        )

        resolved = self._resolve(self.AUTH_Q)

        self.assertNotEqual(resolved.answer, "No, I do not require sponsorship.")
        self.assertTrue(resolved.needs_review)

    def test_saved_authorization_answer_is_not_used_for_the_sponsorship_question(self):
        ExplicitAnswer.objects.create(
            user=self.user, category=ExplicitAnswer.Category.WORK_AUTHORIZATION, answer_text="US Citizen"
        )

        resolved = self._resolve(self.SPONSOR_Q)

        self.assertNotEqual(resolved.answer, "US Citizen")
        self.assertTrue(resolved.needs_review)

    def test_ambiguous_questions_require_review_despite_saved_answers(self):
        for category in (ExplicitAnswer.Category.WORK_AUTHORIZATION, ExplicitAnswer.Category.SPONSORSHIP):
            ExplicitAnswer.objects.create(user=self.user, category=category, answer_text="No")

        for text in (
            "Do you currently hold H-1B status?",
            "Do you currently hold a valid H1B visa?",
            "Do you need H-1B sponsorship?",
            "What is your immigration status?",
            "Do you hold a work permit?",
            "Do you have a visa?",
            "What is your citizenship status?",
            "Are you authorized to work or do you require sponsorship?",
            "Are you eligible to work without visa sponsorship?",
            "Do you have the right to work without sponsorship?",
        ):
            with self.subTest(text=text):
                client = FakeLLMClient()
                question = Question(id="q", text=text, field_type="text")
                resolved = resolve_field_answers(
                    self.user, [question], self.profile.resume_text, self.profile, client
                )[0]
                self.assertIsNone(resolved.answer)
                self.assertTrue(resolved.needs_review)
                self.assertEqual(resolved.reason, ResolutionReason.HARD_EXCLUDED_CATEGORY)
                self.assertEqual(client.calls, [])


class ProfileDerivedSalaryFallbackTests(TestCase):
    """`SALARY_EXPECTATION` is a hard-excluded category (never sent to the
    LLM); with no saved `ExplicitAnswer`, a salary question used to always
    come back blank/needs_review even though `Profile.min_salary` was
    already collected at signup for matching. It should be used as a
    fallback instead -- surfaced for review rather than trusted outright,
    since it was never confirmed as *this* question's exact answer."""

    SALARY_Q = "What is your desired salary?"

    def setUp(self):
        self.user = User.objects.create_user(
            username="grace", password="pw", email="grace@example.com"
        )
        self.profile = self.user.profile
        self.profile.resume_text = "Grace."

    def _resolve(self, text=SALARY_Q, field_type="text", options=None):
        question = Question(id=text, text=text, field_type=field_type, options=options)
        client = FakeLLMClient()
        resolved = resolve_field_answers(
            self.user, [question], self.profile.resume_text, self.profile, client
        )[0]
        return resolved, client

    def test_min_salary_answers_a_salary_question_when_no_explicit_answer(self):
        self.profile.min_salary = 150000
        self.profile.save()

        resolved, client = self._resolve()

        self.assertEqual(resolved.answer, "150000")
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, PROFILE_DERIVED_REASON)
        self.assertEqual(client.calls, [])  # never reaches the LLM either

    def test_explicit_answer_still_wins_over_profile_min_salary(self):
        self.profile.min_salary = 150000
        self.profile.save()
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.SALARY_EXPECTATION,
            answer_text="Negotiable",
        )

        resolved, _ = self._resolve()

        self.assertEqual(resolved.answer, "Negotiable")
        self.assertFalse(resolved.needs_review)
        self.assertEqual(resolved.reason, "explicit_answer")

    def test_no_min_salary_falls_through_to_hard_exclusion_as_before(self):
        self.profile.min_salary = None
        self.profile.save()

        resolved, client = self._resolve()

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.HARD_EXCLUDED_CATEGORY)
        self.assertEqual(client.calls, [])

    def test_min_salary_is_still_option_constrained(self):
        # A salary *range* select shouldn't get the raw number rubber-stamped
        # if it doesn't exactly match a listed range -- same backstop as
        # every other option-bearing answer.
        self.profile.min_salary = 150000
        self.profile.save()

        resolved, _ = self._resolve(
            field_type="single_select", options=("$50k-100k", "$100k-150k", "$150k+"),
        )

        self.assertIsNone(resolved.answer)
        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INVALID_OPTION)
