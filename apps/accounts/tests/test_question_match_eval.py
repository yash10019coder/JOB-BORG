"""The labelled-pair gate for the reworded-question matcher (plan R7, U5).

``near_miss`` pairs must never match; recall on ``reworded`` pairs must reach the target.
The pairs prove only the classes they name: a clean run here is not "zero false merges
in general". ``tuning`` is what thresholds and intent wording were tuned on; ``held_out``
and ``fresh`` are only checked (``held_out`` was consulted while fixing the first-run
gaps; ``fresh`` was written afterwards and run once before further changes).
"""
import copy
import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from apps.accounts import question_matching as qm
from apps.accounts.management.commands import question_match_eval as qme

RECALL_TARGET = 0.8


class LabelledPairsGateTests(SimpleTestCase):
    def setUp(self):
        self.data = qme.load_pairs()
        qme.check_pairs(self.data)

    def test_R7_no_near_miss_pair_matches_at_the_shipped_threshold(self):
        result = qme.evaluate(self.data, qm.DEFAULT_MIN_SCORE)
        for half, row in result.items():
            self.assertEqual(row["false_merges"], [], half)

    def test_R7_recall_on_reworded_pairs_reaches_the_target_in_every_half(self):
        result = qme.evaluate(self.data, qm.DEFAULT_MIN_SCORE)
        for half, row in result.items():
            hit, total = row["recall"]
            self.assertGreaterEqual(hit / total, RECALL_TARGET, f"{half}: missed {row['missed']}")

    def test_R7_the_shipped_threshold_is_the_one_that_was_evaluated(self):
        from django.conf import settings

        self.assertEqual(settings.QUESTION_CANDIDATE_MIN_SCORE, qm.DEFAULT_MIN_SCORE)

    def test_R7_no_near_miss_matches_at_any_threshold_in_the_sweep(self):
        # near-misses are vetoed by guards or the strict rule, not by the threshold,
        # so loosening the threshold must not merge any of them
        for threshold in qme.SWEEP:
            result = qme.evaluate(self.data, threshold)
            for half, row in result.items():
                self.assertEqual(row["false_merges"], [], (threshold, half))

    def test_R7_every_half_has_both_kinds_of_pair(self):
        for half in qme.HALVES:
            self.assertGreaterEqual(len(self.data[half]["reworded"]), 8, half)
            self.assertGreaterEqual(len(self.data[half]["near_miss"]), 10, half)


class FixtureCheckTests(SimpleTestCase):
    def test_a_missing_label_fails_loudly(self):
        data = copy.deepcopy(qme.load_pairs())
        data["tuning"]["near_miss"][0].pop("label")
        with self.assertRaises(ValueError):
            qme.check_pairs(data)

    def test_an_empty_list_is_not_a_vacuous_pass(self):
        data = copy.deepcopy(qme.load_pairs())
        data["held_out"]["reworded"] = []
        with self.assertRaises(ValueError):
            qme.check_pairs(data)

    def test_adding_a_near_miss_the_matcher_accepts_fails_the_gate(self):
        data = copy.deepcopy(qme.load_pairs())
        data["tuning"]["near_miss"].append(
            {"label": "actually the same", "a": "What is your notice period?", "b": "Notice period?"}
        )
        result = qme.evaluate(data, qm.DEFAULT_MIN_SCORE)
        self.assertEqual(result["tuning"]["false_merges"], ["actually the same"])


class CommandTests(SimpleTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("question_match_eval", *args, stdout=out)
        return out.getvalue()

    def test_the_command_prints_a_table_and_exits_zero(self):
        text = self.run_command()
        for half in qme.HALVES:
            self.assertIn(half, text)
        self.assertIn("false merges 0", text)

    def test_the_sweep_lists_every_threshold(self):
        text = self.run_command("--sweep")
        for threshold in qme.SWEEP:
            self.assertIn(f"{threshold}:", text)

    def test_json_output_parses(self):
        parsed = json.loads(self.run_command("--json"))
        self.assertEqual(set(parsed["result"]), set(qme.HALVES))

    def test_a_false_merge_makes_the_command_fail(self):
        data = copy.deepcopy(qme.load_pairs())
        data["tuning"]["near_miss"].append(
            {"label": "same", "a": "What is your notice period?", "b": "Notice period?"}
        )
        with mock.patch.object(qme, "load_pairs", return_value=data):
            with self.assertRaises(CommandError):
                self.run_command()

    def test_an_empty_fixture_makes_the_command_fail(self):
        with mock.patch.object(qme, "load_pairs", return_value={}):
            with self.assertRaises(CommandError):
                self.run_command()

    @override_settings(QUESTION_CANDIDATE_MIN_SCORE=0.9)
    def test_the_threshold_comes_from_the_setting(self):
        self.assertIn("Threshold 0.9", self.run_command())
