"""Tests for the rule-based question-category classifier (U4).

Covers the HARD_EXCLUDED_CATEGORIES/classify contract that
``apps.auto_apply.llm.base.resolve_answers`` relies on as a hard security
boundary, plus a representative sweep of real-world custom-question
phrasings across all categories.
"""
from django.test import SimpleTestCase

from apps.auto_apply.llm.categories import (
    HARD_EXCLUDED_CATEGORIES,
    QuestionCategory,
    classify,
)


class HardExcludedCategoriesContractTests(SimpleTestCase):
    def test_hard_excluded_categories_are_exactly_the_sensitive_four(self):
        self.assertEqual(
            HARD_EXCLUDED_CATEGORIES,
            frozenset(
                {
                    QuestionCategory.WORK_AUTHORIZATION,
                    QuestionCategory.LEGAL_ATTESTATION,
                    QuestionCategory.BACKGROUND_CHECK,
                    QuestionCategory.SALARY_EXPECTATION,
                }
            ),
        )

    def test_generic_is_not_hard_excluded(self):
        self.assertNotIn(QuestionCategory.GENERIC, HARD_EXCLUDED_CATEGORIES)


class WorkAuthorizationClassificationTests(SimpleTestCase):
    def test_sponsorship_question(self):
        self.assertEqual(
            classify("Will you now or in the future require visa sponsorship to work in the US?"),
            QuestionCategory.WORK_AUTHORIZATION,
        )

    def test_authorized_to_work_question(self):
        self.assertEqual(
            classify("Are you legally authorized to work in the United States?"),
            QuestionCategory.WORK_AUTHORIZATION,
        )

    def test_h1b_question(self):
        self.assertEqual(
            classify("Do you currently hold a valid H-1B visa?"),
            QuestionCategory.WORK_AUTHORIZATION,
        )


class LegalAttestationClassificationTests(SimpleTestCase):
    def test_perjury_attestation(self):
        self.assertEqual(
            classify(
                "I certify that the information provided in this application is true "
                "and accurate to the best of my knowledge, under penalty of perjury."
            ),
            QuestionCategory.LEGAL_ATTESTATION,
        )

    def test_non_compete_question(self):
        self.assertEqual(
            classify("Are you currently subject to a non-compete agreement?"),
            QuestionCategory.LEGAL_ATTESTATION,
        )


class BackgroundCheckClassificationTests(SimpleTestCase):
    def test_background_check_consent_question(self):
        self.assertEqual(
            classify("Do you consent to a background check as a condition of employment?"),
            QuestionCategory.BACKGROUND_CHECK,
        )

    def test_criminal_history_question(self):
        self.assertEqual(
            classify("Have you ever been convicted of a felony?"),
            QuestionCategory.BACKGROUND_CHECK,
        )


class SalaryExpectationClassificationTests(SimpleTestCase):
    def test_salary_expectations_question(self):
        self.assertEqual(
            classify("What are your salary expectations for this role?"),
            QuestionCategory.SALARY_EXPECTATION,
        )

    def test_current_compensation_question(self):
        self.assertEqual(
            classify("What is your current total compensation?"),
            QuestionCategory.SALARY_EXPECTATION,
        )

    def test_desired_salary_question(self):
        self.assertEqual(
            classify("Desired salary?"),
            QuestionCategory.SALARY_EXPECTATION,
        )


class GenericFitClassificationTests(SimpleTestCase):
    def test_why_this_company_question(self):
        self.assertEqual(
            classify("Why do you want to work at our company?"),
            QuestionCategory.GENERIC,
        )

    def test_years_of_experience_question(self):
        self.assertEqual(
            classify("How many years of experience do you have with Python?"),
            QuestionCategory.GENERIC,
        )

    def test_most_recent_employer_question(self):
        self.assertEqual(
            classify("What company did you most recently work at?"),
            QuestionCategory.GENERIC,
        )

    def test_empty_question_text(self):
        self.assertEqual(classify(""), QuestionCategory.GENERIC)

    def test_none_question_text(self):
        self.assertEqual(classify(None), QuestionCategory.GENERIC)


class ClassificationIsCaseInsensitiveTests(SimpleTestCase):
    def test_uppercase_sponsorship_question(self):
        self.assertEqual(
            classify("WILL YOU REQUIRE VISA SPONSORSHIP?"),
            QuestionCategory.WORK_AUTHORIZATION,
        )
