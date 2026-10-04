import ast
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.accounts import tiering
from apps.accounts.tiering import Tier, classify, classify_tier, higher_tier

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "tier_corpus.json").read_text()
)


class TierCorpusTests(SimpleTestCase):
    def test_every_corpus_question_gets_its_labelled_tier(self):
        wrong = [
            (row["text"], row["tier"], classify_tier(row["text"]))
            for row in CORPUS
            if classify_tier(row["text"]) != row["tier"]
        ]
        self.assertEqual(wrong, [])

    def test_t0_and_t1_recall_is_total(self):
        """No T0/T1 question may ever be downgraded to T2."""
        downgraded = [
            row["text"]
            for row in CORPUS
            if row["tier"] != Tier.T2_FACTUAL
            and classify_tier(row["text"]) == Tier.T2_FACTUAL
        ]
        self.assertEqual(downgraded, [])


class TierRuleTests(SimpleTestCase):
    def test_compound_question_takes_the_higher_tier(self):
        self.assertEqual(
            classify_tier("5 years of experience and are you authorized to work?"),
            Tier.T0_LEGAL,
        )

    def test_t2_needs_a_positive_match(self):
        self.assertEqual(classify_tier("Gibberish xyz"), Tier.T0_LEGAL)
        self.assertEqual(classify_tier(""), Tier.T0_LEGAL)
        self.assertEqual(classify_tier(None), Tier.T0_LEGAL)

    def test_salary_is_t1_and_relocation_beats_location(self):
        self.assertEqual(classify_tier("Desired salary"), Tier.T1_COMMERCIAL)
        self.assertEqual(
            classify_tier("Are you based in, or open to relocate to Japan?"),
            Tier.T1_COMMERCIAL,
        )

    def test_higher_tier_orders_t0_over_t1_over_t2(self):
        self.assertEqual(higher_tier(Tier.T2_FACTUAL, Tier.T1_COMMERCIAL), Tier.T1_COMMERCIAL)
        self.assertEqual(higher_tier(Tier.T1_COMMERCIAL, Tier.T0_LEGAL), Tier.T0_LEGAL)
        self.assertEqual(higher_tier(Tier.T0_LEGAL, Tier.T2_FACTUAL), Tier.T0_LEGAL)

    def test_every_hard_excluded_category_is_at_least_t1(self):
        for category in tiering.HARD_EXCLUDED_CATEGORIES:
            self.assertIn(
                tiering._CATEGORY_TIER[category], {Tier.T0_LEGAL, Tier.T1_COMMERCIAL}
            )

    def test_category_classifier_is_unchanged_by_the_move(self):
        self.assertEqual(
            classify("Are you authorized to work in the US?"),
            tiering.QuestionCategory.WORK_AUTHORIZATION,
        )
        self.assertEqual(classify("Favourite colour?"), tiering.QuestionCategory.GENERIC)


class LeafModuleTests(SimpleTestCase):
    def test_module_imports_nothing_from_other_apps_or_django(self):
        tree = ast.parse(Path(tiering.__file__).read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertEqual(imported, {"re"})


class TierV2Tests(SimpleTestCase):
    def test_petition_and_employment_based_status_are_work_authorization(self):
        text = (
            "Will you now or in the future require the company to file a petition "
            "or application for employment-based status?"
        )
        self.assertEqual(classify(text), tiering.QuestionCategory.WORK_AUTHORIZATION)
        self.assertEqual(classify_tier(text), Tier.T0_LEGAL)

    def test_competition_is_not_a_petition(self):
        self.assertNotEqual(
            classify("Have you entered a coding competition?"),
            tiering.QuestionCategory.WORK_AUTHORIZATION,
        )

    def test_where_the_user_lives_is_a_plain_fact(self):
        for text in (
            "What country are you located in?",
            "Which country do you currently work in?",
            "Where do you currently live?",
        ):
            self.assertEqual(classify_tier(text), Tier.T2_FACTUAL, text)

    def test_location_membership_and_relocation_stay_above_t2(self):
        self.assertEqual(
            classify_tier("Are you currently located in Argentina, Uruguay or Chile?"),
            Tier.T0_LEGAL,
        )
        self.assertEqual(
            classify_tier("Are you willing to relocate to another country?"),
            Tier.T1_COMMERCIAL,
        )

    def test_version_was_bumped_with_the_pattern_change(self):
        self.assertEqual(tiering.TIERING_VERSION, "v2")
