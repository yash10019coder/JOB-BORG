"""Candidate observations, precision reporting and the learner exclusion (Phase 4b, plan U4).

Measure-only: nothing here changes what a draft contains.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import AnswerObservation
from apps.accounts.services.answer_resolver import normalize_question_key, write_answer
from apps.accounts.services.learning import learn_for_profile
from apps.accounts.services.shadow_metrics import candidate_stats, wilson_lower_bound
from apps.accounts.tests.test_learning import HEARD, _Base
from apps.accounts.tiering import Tier

CAND_KEY = normalize_question_key("Where did you hear about this job?")


class CandidateStatsTests(_Base):
    def shown(self, *, equal, accepted=False, tier=Tier.T2_FACTUAL, key=CAND_KEY, value="x"):
        obs = self.observe(HEARD, value, "Acme", tier=tier)
        AnswerObservation.objects.filter(pk=obs.pk).update(
            candidate_question_key=key, candidate_equal=equal, candidate_accepted=accepted
        )

    def test_precision_counts_only_typed_or_changed_answers(self):
        for equal in (True, True, False):
            self.shown(equal=equal)
        for _ in range(2):
            self.shown(equal=True, accepted=True)
        stats = candidate_stats(self.profile)[Tier.T2_FACTUAL]
        self.assertEqual((stats.shown, stats.typed, stats.matched, stats.accepted), (5, 3, 2, 2))
        self.assertAlmostEqual(stats.precision, 2 / 3)
        self.assertAlmostEqual(stats.lower_bound, wilson_lower_bound(2, 3))
        self.assertAlmostEqual(stats.accept_rate, 2 / 5)

    def test_clicking_through_cannot_raise_the_number(self):
        """QR3."""
        for _ in range(10):
            self.shown(equal=True, accepted=True)
        stats = candidate_stats(self.profile)[Tier.T2_FACTUAL]
        self.assertIsNone(stats.precision)
        self.assertEqual(stats.lower_bound, 0.0)
        self.assertEqual(stats.accept_rate, 1.0)

    def test_a_candidate_from_an_answer_saved_through_an_accept_is_left_out(self):
        write_answer(
            self.profile, "Where did you hear about this job?", "LinkedIn", "user",
            source_detail={"via_candidate": True},
        )
        self.shown(equal=True)
        stats = candidate_stats(self.profile)[Tier.T2_FACTUAL]
        self.assertEqual((stats.typed, stats.self_reinforced), (0, 1))

    def test_tiers_are_reported_separately(self):
        self.shown(equal=True, tier=Tier.T2_FACTUAL)
        self.shown(equal=False, tier=Tier.T1_COMMERCIAL)
        stats = candidate_stats(self.profile)
        self.assertEqual(set(stats), {Tier.T2_FACTUAL, Tier.T1_COMMERCIAL})

    def test_observations_without_a_candidate_and_other_profiles_are_not_counted(self):
        self.observe(HEARD, "x", "Acme")
        other = User_profile("bob")
        self.observe(HEARD, "x", "Acme", profile=other)
        self.assertEqual(candidate_stats(self.profile), {})

    def test_no_observations_is_empty_not_an_error(self):
        self.assertEqual(candidate_stats(), {})


def User_profile(name):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username=name, password="pw").profile


class LearnerExclusionTests(_Base):
    def test_an_accepted_candidate_is_not_a_vote(self):
        """QR3."""
        first = self.observe(HEARD, "LinkedIn", "Acme")
        self.observe(HEARD, "LinkedIn", "Beta")
        AnswerObservation.objects.filter(pk=first.pk).update(candidate_accepted=True)
        learn_for_profile(self.profile)
        self.assertIsNone(self.learned(HEARD))

    def test_control_the_same_two_typed_answers_are_learned(self):
        self.observe(HEARD, "LinkedIn", "Acme")
        self.observe(HEARD, "LinkedIn", "Beta")
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(HEARD).value, "LinkedIn")

    def test_observations_stay_append_only(self):
        obs = self.observe(HEARD, "LinkedIn", "Acme")
        obs.candidate_accepted = True
        with self.assertRaises(ValueError):
            obs.save()


class ReportTests(_Base):
    def test_the_report_prints_counts_and_no_question_text_or_values(self):
        """QR5."""
        obs = self.observe(HEARD, "SecretValue", "Acme")
        AnswerObservation.objects.filter(pk=obs.pk).update(
            candidate_question_key=CAND_KEY, candidate_equal=True
        )
        out = StringIO()
        call_command("learning_report", stdout=out)
        text = out.getvalue()
        self.assertIn("Reworded-question candidates", text)
        self.assertIn("shown=1", text)
        self.assertNotIn("SecretValue", text)
        self.assertNotIn(HEARD, text)

    def test_the_report_says_so_when_nothing_was_shown(self):
        out = StringIO()
        call_command("learning_report", stdout=out)
        self.assertIn("no candidates shown yet", out.getvalue())
