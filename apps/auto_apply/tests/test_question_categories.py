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
    def test_hard_excluded_categories_are_exactly_the_sensitive_categories(self):
        self.assertEqual(
            HARD_EXCLUDED_CATEGORIES,
            frozenset(
                {
                    QuestionCategory.WORK_AUTHORIZATION,
                    QuestionCategory.LEGAL_ATTESTATION,
                    QuestionCategory.BACKGROUND_CHECK,
                    QuestionCategory.SALARY_EXPECTATION,
                    QuestionCategory.DEMOGRAPHIC,
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


class DemographicClassificationTests(SimpleTestCase):
    def test_self_identification_questions_are_demographic_and_hard_excluded(self):
        for text in (
            "What is your gender?",
            "What is your sex?",
            "What is your religion?",
            "What is your religious affiliation?",
            "Do you identify as LGBTQ+?",
            "Do you identify as LGBTQIA+?",
            "What is your national origin?",
            "Are you a member of a protected class?",
            "Please disclose protected-class membership.",
            "What is your age?",
            "How old are you?",
            "Are you at least 18 years old?",
            "Please select your race/ethnicity",
            "Are you a protected veteran?",
            "Do you have a disability?",
            "What are your pronouns?",
            "Are you Hispanic or Latino?",
            "What is your sexual orientation?",
        ):
            with self.subTest(text=text):
                category = classify(text)
                self.assertEqual(category, QuestionCategory.DEMOGRAPHIC)
                self.assertIn(category, HARD_EXCLUDED_CATEGORIES)

    def test_earlier_categories_keep_first_match_precedence(self):
        for text, expected in (
            ("Are you authorized to work regardless of national origin?", QuestionCategory.WORK_AUTHORIZATION),
            ("I certify that my age is accurate", QuestionCategory.LEGAL_ATTESTATION),
            ("Do you consent to an age and background check?", QuestionCategory.BACKGROUND_CHECK),
            ("What salary do you expect at your age?", QuestionCategory.SALARY_EXPECTATION),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(text), expected)

    def test_bare_salary_wording_is_salary_expectation(self):
        self.assertEqual(classify("What salary are you looking for?"), QuestionCategory.SALARY_EXPECTATION)

    def test_ordinary_questions_stay_generic(self):
        for text in ("Why do you want to work here?", "Are you 100% available for this role?"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), QuestionCategory.GENERIC)


class AdditionalSensitiveQuestionTests(SimpleTestCase):
    def test_demographics_are_excluded(self):
        for question in ("Gender", "Race/ethnicity", "Veteran status", "Disability status", "Sexual orientation", "Are you Hispanic or Latino?"):
            with self.subTest(question=question):
                self.assertEqual(classify(question), QuestionCategory.DEMOGRAPHIC)
                self.assertIn(classify(question), HARD_EXCLUDED_CATEGORIES)

    def test_compensation_variants_are_excluded(self):
        for question in ("Salary requirements?", "Compensation requirement", "Desired compensation", "Desired pay"):
            with self.subTest(question=question):
                self.assertEqual(classify(question), QuestionCategory.SALARY_EXPECTATION)
                self.assertIn(classify(question), HARD_EXCLUDED_CATEGORIES)
