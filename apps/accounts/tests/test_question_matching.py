"""Rules QM1-QM11: when one question is a plausible candidate for another."""
import ast
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.accounts import question_matching as qm

FIXTURE = Path(__file__).parent / "fixtures" / "question_pairs.json"


def load_pairs():
    return json.loads(FIXTURE.read_text())


class ComparisonTextTests(SimpleTestCase):
    def test_QM1_decoration_and_case_do_not_change_the_text(self):
        for text in ("Phone number *", "PHONE NUMBER", "Please phone number (optional)", "phone, number"):
            self.assertEqual(qm.comparable(text).replace("please ", ""), "phone number", text)

    def test_QM1_contractions_become_negations(self):
        self.assertEqual(qm.comparable("Don't you work?"), "do not you work")

    def test_QM1_text_is_bounded(self):
        self.assertLessEqual(len(qm.comparable("a " * 5000)), qm.MAX_COMPARE_CHARS)

    def test_QM1_odd_input_never_raises(self):
        for value in (None, "", "   ", "?!", "​", "\U0001F600" * 50, 12, b"x".decode()):
            self.assertFalse(qm.match(value, "What is your notice period?").matched)
            self.assertFalse(qm.match("What is your notice period?", value).matched)


class TrigramTests(SimpleTestCase):
    def test_QM2_identical_text_scores_one_and_disjoint_scores_zero(self):
        self.assertEqual(qm.similarity("notice period", "notice period"), 1.0)
        self.assertEqual(qm.similarity("abc", "xyz"), 0.0)

    def test_QM2_empty_text_scores_zero(self):
        self.assertEqual(qm.similarity("", "notice"), 0.0)

    def test_QM2_trigrams_are_word_padded(self):
        self.assertIn("  n", qm.trigrams("notice"))
        self.assertIn("ce ", qm.trigrams("notice"))

    def test_QM2_a_closer_wording_scores_higher(self):
        near = qm.similarity("notice period", "what is your notice period")
        far = qm.similarity("notice period", "are you willing to relocate")
        self.assertGreater(near, far)


class GuardTests(SimpleTestCase):
    def test_QM3_typed_owned_questions_never_get_candidates(self):
        result = qm.match("What is your citizenship?", "Which country are you a citizen of?")
        self.assertEqual(result.vetoed, "typed_owned")
        self.assertFalse(result.matched)

    def test_QM4_negation_vetoes_even_at_full_similarity(self):
        result = qm.match("Are you authorized to work in the US?", "Are you NOT authorized to work in the US?")
        self.assertFalse(result.matched)
        self.assertEqual(result.vetoed, "polarity")

    def test_QM4_contracted_negation_is_a_negation(self):
        result = qm.match("Do you require sponsorship?", "Don't you require sponsorship?")
        self.assertEqual(result.vetoed, "polarity")

    def test_QM5_authorization_and_sponsorship_are_never_paired(self):
        result = qm.match("Are you authorized to work in the US?", "Will you require sponsorship in the US?")
        self.assertEqual(result.vetoed, "auth_kind")

    def test_QM5_status_wording_is_ambiguous_and_vetoed(self):
        result = qm.match("Are you on an H-1B?", "Are you on an H-1B visa?")
        self.assertEqual(result.vetoed, "auth_kind")

    def test_QM5_immigration_sponsorship_still_pairs_with_other_sponsorship_wordings(self):
        result = qm.match(
            "Do you require immigration sponsorship to work in the US?",
            "Will you now or in the future require sponsorship to work in the US?",
        )
        self.assertTrue(result.matched, result)

    def test_QM6_different_named_countries_or_abbreviations_veto(self):
        self.assertEqual(
            qm.match("Do you have a US passport?", "Do you have a UK passport?").vetoed, "countries"
        )
        self.assertEqual(
            qm.match("Can you work in Germany?", "Can you work in France?").vetoed, "countries"
        )

    def test_QM6_a_country_on_one_side_only_vetoes(self):
        result = qm.match("Will you require sponsorship?", "Will you require sponsorship in the UK?")
        self.assertEqual(result.vetoed, "countries")

    def test_QM7_different_tiers_veto_without_a_shared_intent(self):
        # a salary question (T1) and a portfolio question (T2)
        result = qm.match("What are your salary expectations?", "What is your portfolio website?")
        self.assertFalse(result.matched)

    def test_QM7_a_shared_intent_lets_the_riskier_tier_govern(self):
        # the classifier files the second wording as unknown (T0), the first as T1
        result = qm.match("What is your notice period?", "How much notice do you need to give your current employer?")
        self.assertTrue(result.matched)
        self.assertEqual(result.via, "intent")

    def test_QM8_different_numbers_veto(self):
        result = qm.match("Are you at least 18 years of age?", "Are you at least 21 years of age?")
        self.assertEqual(result.vetoed, "numbers")

    def test_QM12_a_swapped_name_vetoes(self):
        result = qm.match("Do you have experience with Python?", "Do you have experience with Java?")
        self.assertEqual(result.vetoed, "named_terms")

    def test_QM12_shouting_slashes_and_title_case_are_not_names(self):
        self.assertEqual(qm._named_terms("What is your ETHNICITY?"), frozenset())
        self.assertEqual(qm._named_terms("Race/Ethnicity"), frozenset())
        self.assertEqual(qm._named_terms("What Is Your Notice Period Right Now"), frozenset())
        self.assertEqual(qm._named_terms("Do you know Python and Django?"), frozenset({"python", "django"}))

    def test_QM13_two_different_intents_veto(self):
        self.assertEqual(qm.match("What is your gender?", "What is your race?").vetoed, "intent")
        self.assertEqual(qm.match("What is your notice period?", "What is your start date?").vetoed, "intent")

    def test_QM3_to_QM8_a_veto_wins_over_a_perfect_score(self):
        result = qm.match("Are you authorized to work in the US?", "Are you NOT authorized to work in the US?")
        self.assertGreater(result.score, 0.8)
        self.assertFalse(result.matched)


class StrictTierTests(SimpleTestCase):
    def test_QM9_one_qualifier_word_vetoes_a_legal_pair(self):
        for a, b in (
            ("Have you ever been terminated from a job?", "Have you ever been laid off from a job?"),
            ("Have you been convicted of a felony?", "Have you been convicted of a misdemeanor?"),
            ("Have you ever been convicted of a crime?", "Have you ever been charged with a crime?"),
        ):
            result = qm.match(a, b)
            self.assertFalse(result.matched, (a, b))
            self.assertEqual(result.vetoed, "strict_tier", (a, b))

    def test_QM9_current_vs_previous_employer_is_vetoed(self):
        result = qm.match("Are you currently employed by Acme?", "Were you previously employed by Acme?")
        self.assertFalse(result.matched)

    def test_QM9_an_unrecognised_question_is_held_to_the_strict_rule(self):
        # the classifier files an unknown question as legal (T0)
        result = qm.match("Do you enjoy working with llamas?", "Do you enjoy working with alpacas?")
        self.assertFalse(result.matched)

    def test_QM9_identical_meaningful_words_match(self):
        result = qm.match("Are you willing to relocate?", "Are you willing to relocate")
        self.assertTrue(result.matched)
        self.assertEqual(result.via, "words")

    def test_QM9_same_intent_with_only_listed_wording_differences_matches(self):
        result = qm.match("Notice period?", "What is your notice period?")
        self.assertTrue(result.matched)

    def test_QM9_same_intent_with_an_extra_meaningful_word_does_not(self):
        result = qm.match("What is your notice period?", "What is your notice period at your last employer?")
        self.assertFalse(result.matched)


class IntentTests(SimpleTestCase):
    def test_QM10_common_questions_have_an_intent(self):
        cases = {
            "What is your notice period?": "notice_period",
            "When can you start?": "start_date",
            "Are you willing to relocate?": "relocation",
            "How did you hear about us?": "referral_source",
            "Gender": "gender",
            "What is your ethnicity?": "race_ethnicity",
            "Are you a veteran?": "veteran",
            "Do you have a disability?": "disability",
            "Do you require visa sponsorship?": "sponsorship",
            "Are you authorized to work in the US?": "authorization",
            "How many years of experience do you have with Python?": "years_of:python",
            "Years of Django experience": "years_of:django",
        }
        for text, intent in cases.items():
            self.assertEqual(qm.intent_of(text), intent, text)

    def test_QM10_compound_ambiguous_or_unrelated_text_has_no_intent(self):
        for text in (
            "What is your gender and race?",
            "Are you authorized to work in the US and will you require sponsorship?",
            "Are you NOT authorized to work in the US?",
            "Why do you want this job?",
            "",
            None,
        ):
            self.assertIsNone(qm.intent_of(text), text)

    def test_QM10_privacy_notice_is_not_a_notice_period(self):
        self.assertIsNone(qm.intent_of("Do you accept the privacy notice?"))


class ScoreOutsideStrictTiersTests(SimpleTestCase):
    def test_QM11_a_close_t2_wording_matches_on_trigram_score(self):
        result = qm.match("LinkedIn profile URL", "Your LinkedIn profile")
        self.assertTrue(result.matched, result)
        self.assertEqual(result.via, "trigram")

    def test_QM11_a_distant_t2_wording_does_not(self):
        self.assertFalse(qm.match("LinkedIn profile URL", "Phone number").matched)

    def test_QM11_the_threshold_is_a_parameter(self):
        pair = ("LinkedIn profile URL", "Your LinkedIn profile")
        self.assertTrue(qm.match(*pair, min_score=0.1).matched)
        self.assertFalse(qm.match(*pair, min_score=0.99).matched)


class LabelledPairsTests(SimpleTestCase):
    """The fixture loads and every guard has at least one near-miss in it.

    Recall and zero-false-merge assertions over the whole set live in
    ``test_question_match_eval`` (U5). The near-miss set proves only the
    classes it names.
    """

    def test_the_fixture_is_well_formed(self):
        data = load_pairs()
        for half in ("tuning", "held_out"):
            for kind in ("reworded", "near_miss"):
                pairs = data[half][kind]
                self.assertTrue(pairs, (half, kind))
                for pair in pairs:
                    self.assertTrue(pair["label"] and pair["a"] and pair["b"], pair)

    def test_every_near_miss_is_rejected(self):
        data = load_pairs()
        for half in ("tuning", "held_out"):
            for pair in data[half]["near_miss"]:
                self.assertFalse(qm.match(pair["a"], pair["b"]).matched, pair["label"])

    def test_every_guard_has_a_near_miss_that_trips_it(self):
        data = load_pairs()
        seen = {
            qm.match(p["a"], p["b"]).vetoed
            for half in ("tuning", "held_out")
            for p in data[half]["near_miss"]
        }
        for guard in ("polarity", "auth_kind", "countries", "numbers", "named_terms", "intent", "strict_tier"):
            self.assertIn(guard, seen)


class LeafBoundaryTests(SimpleTestCase):
    def test_R9_the_matcher_imports_only_the_two_leaves(self):
        tree = ast.parse(Path(qm.__file__).read_text())
        imported = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [None])
        }
        for name in imported:
            self.assertFalse(name and name.startswith(("django", "apps.auto_apply", "apps.web", "apps.jobs")), name)
            self.assertNotIn(name, {"apps.accounts.models"})
