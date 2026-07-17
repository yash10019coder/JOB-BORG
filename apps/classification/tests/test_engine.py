"""Tests for the classification engine — fully deterministic, no DB/network."""
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.classification.engine import classify, load_ruleset
from apps.classification.rule_types import RuleConfigError, evaluate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_jobs.json"


class RulesetFixtureTests(SimpleTestCase):
    def test_committed_sample_jobs_produce_expected_tags(self):
        for case in json.loads(FIXTURE.read_text()):
            with self.subTest(case=case["name"]):
                self.assertEqual(classify(case["job"]), case["expected_tags"])


class RuleTypeTests(SimpleTestCase):
    def test_keyword_rule_matches(self):
        job = {"title": "SRE", "description": "kubernetes clusters"}
        self.assertIn("kubernetes", classify(job))
        self.assertIn("devops", classify(job))

    def test_regex_seniority_respects_word_boundary(self):
        self.assertIn("senior", classify({"title": "Senior Engineer", "description": ""}))
        # "senioritis" must not trigger the seniority tag.
        self.assertNotIn(
            "senior", classify({"title": "Cure for senioritis", "description": ""})
        )

    def test_field_rule_independent_of_text(self):
        job = {"title": "Analyst", "description": "no keywords here", "is_remote": True}
        self.assertIn("remote", classify(job))

    def test_no_match_returns_empty_list(self):
        self.assertEqual(classify({"title": "Barista", "description": "make coffee"}), [])

    def test_case_insensitive_matching(self):
        self.assertIn("python", classify({"title": "DJANGO dev", "description": ""}))

    def test_salary_threshold(self):
        self.assertIn(
            "high_comp", classify({"title": "x", "description": "", "salary_min": 200000})
        )
        self.assertNotIn(
            "high_comp", classify({"title": "x", "description": "", "salary_min": 90000})
        )
        # Unknown salary never trips the threshold.
        self.assertNotIn(
            "high_comp", classify({"title": "x", "description": "", "salary_min": None})
        )


class DeterminismAndVersioningTests(SimpleTestCase):
    def test_tags_are_deduped_and_sorted(self):
        # Kubernetes trips both `kubernetes` and `devops`; output is sorted.
        tags = classify({"title": "Senior SRE", "description": "kubernetes terraform"})
        self.assertEqual(tags, sorted(tags))
        self.assertEqual(len(tags), len(set(tags)))

    def test_identical_input_yields_identical_output(self):
        job = {"title": "Senior Backend", "description": "python kubernetes"}
        self.assertEqual(classify(job), classify(job))

    def test_two_ruleset_versions_produce_differing_tags(self):
        job = {"title": "Rust Systems Engineer", "description": "we write rust"}
        v1 = classify(job, load_ruleset("v1"))
        # A hypothetical v2 that adds a rust rule tags this job differently.
        v2 = {
            "version": "v2-test",
            "rules": [{"tag": "rust", "type": "keyword_any", "keywords": ["rust"]}],
        }
        self.assertEqual(v1, [])
        self.assertEqual(classify(job, v2), ["rust"])

    def test_unknown_rule_type_raises(self):
        with self.assertRaises(RuleConfigError):
            evaluate({"tag": "x", "type": "no_such_type"}, {"title": "", "description": ""})

    def test_missing_ruleset_version_raises(self):
        with self.assertRaises(RuleConfigError):
            load_ruleset("does-not-exist")
