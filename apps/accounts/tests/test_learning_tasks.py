from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts import tasks
from apps.accounts.models import AnswerBank, AnswerObservation, ProfileSuggestion
from apps.accounts.services.answer_resolver import normalize_question_key
from apps.accounts.services.shadow_metrics import shadow_stats, wilson_lower_bound

User = get_user_model()
HEARD = "How did you hear about us?"


def observe(profile, text, value, employer, *, learned_value=None, origin="", **kwargs):
    return AnswerObservation.objects.create(
        profile=profile, question_key=normalize_question_key(text), question_text=text,
        value=value, tier="t2_factual", field_type="text", user_edited=True,
        job_id=AnswerObservation.objects.count() + 1, employer_name=employer,
        job_region="US", learned_value=learned_value, provenance_origin=origin, **kwargs,
    )


class LearnTaskTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def test_the_task_learns_for_one_profile(self):
        observe(self.profile, HEARD, "x", "Acme")
        observe(self.profile, HEARD, "x", "Beta")
        result = tasks.learn_for_profile_task(self.profile.pk)
        self.assertEqual(result["written"], 1)
        self.assertEqual(AnswerBank.objects.get().source, "learned")

    def test_a_missing_profile_is_logged_and_ignored(self):
        with self.assertLogs("apps.accounts.tasks", level="WARNING"):
            self.assertIsNone(tasks.learn_for_profile_task(999999))

    def test_the_task_is_idempotent(self):
        observe(self.profile, HEARD, "x", "Acme")
        observe(self.profile, HEARD, "x", "Beta")
        tasks.learn_for_profile_task(self.profile.pk)
        second = tasks.learn_for_profile_task(self.profile.pk)
        self.assertEqual(second["written"], 0)
        self.assertEqual(AnswerBank.objects.count(), 1)


class SweepTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="pw").profile
        self.bob = User.objects.create_user(username="bob", password="pw").profile

    def _evidence(self, profile):
        observe(profile, HEARD, "x", "Acme")
        observe(profile, HEARD, "x", "Beta")

    def test_only_profiles_with_recent_evidence_are_processed(self):
        self._evidence(self.alice)
        self._evidence(self.bob)
        AnswerObservation.objects.filter(profile=self.bob).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        result = tasks.sweep_learning()
        self.assertEqual(result["profiles"], 1)
        self.assertTrue(AnswerBank.objects.filter(profile=self.alice).exists())
        self.assertFalse(AnswerBank.objects.filter(profile=self.bob).exists())

    def test_one_failing_profile_does_not_stop_the_batch(self):
        self._evidence(self.alice)
        self._evidence(self.bob)
        from apps.accounts.services import learning

        original = learning.learn_for_profile

        def flaky(profile, **kwargs):
            if profile.pk == self.alice.pk:
                raise RuntimeError("boom")
            return original(profile, **kwargs)

        with mock.patch("apps.accounts.services.learning.learn_for_profile", flaky), \
                self.assertLogs("apps.accounts.tasks", level="ERROR"):
            result = tasks.sweep_learning()
        self.assertEqual((result["profiles"], result["failed"]), (1, 1))
        self.assertTrue(AnswerBank.objects.filter(profile=self.bob).exists())

    def test_batch_size_limits_the_sweep(self):
        self._evidence(self.alice)
        self._evidence(self.bob)
        with self.settings(LEARNING_SWEEP_BATCH_SIZE=1):
            self.assertEqual(tasks.sweep_learning()["profiles"], 1)

    def test_stale_suggestions_are_expired_by_the_sweep(self):
        ProfileSuggestion.objects.create(
            profile=self.bob, question_key="q", value="x", value_fingerprint="f", tier="t1_commercial"
        )
        ProfileSuggestion.objects.update(created_at=timezone.now() - timedelta(days=100))
        tasks.sweep_learning()
        self.assertEqual(ProfileSuggestion.objects.get().status, "expired")

    def test_the_nightly_entry_is_scheduled(self):
        entry = settings.CELERY_BEAT_SCHEDULE["learning-sweep-nightly"]
        self.assertEqual(entry["task"], "apps.accounts.sweep_learning")


class ShadowMetricsTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def test_precision_counts_unchanged_submissions_and_skips_unprefilled_ones(self):
        for _ in range(3):
            observe(self.profile, HEARD, "A", "E", learned_value="A")
        observe(self.profile, HEARD, "B", "E", learned_value="A")
        observe(self.profile, HEARD, "A", "E")  # no learned prefill: not counted
        stats = shadow_stats(self.profile)[normalize_question_key(HEARD)]
        self.assertEqual((stats.observations, stats.matched), (4, 3))
        self.assertEqual(stats.precision, 0.75)
        self.assertLess(stats.lower_bound, 0.75)

    def test_comparison_is_case_and_whitespace_insensitive(self):
        observe(self.profile, HEARD, " linkedin", "E", learned_value="LinkedIn")
        self.assertEqual(shadow_stats(self.profile)[normalize_question_key(HEARD)].matched, 1)

    def test_blind_bulk_confirmations_are_counted_separately(self):
        observe(self.profile, HEARD, "A", "E", learned_value="A", origin="draft_review_bulk")
        observe(self.profile, HEARD, "A", "E", learned_value="A", origin="draft_review")
        stats = shadow_stats(self.profile)[normalize_question_key(HEARD)]
        self.assertEqual((stats.observations, stats.bulk_confirmed), (2, 1))

    def test_wilson_bound_edges(self):
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)
        self.assertGreater(wilson_lower_bound(100, 100), 0.96)
        self.assertLess(wilson_lower_bound(5, 5), wilson_lower_bound(100, 100))

    def test_the_report_command_prints_precision_and_the_dry_run(self):
        observe(self.profile, HEARD, "A", "E1", learned_value="A")
        observe(self.profile, HEARD, "A", "E2")
        out = StringIO()
        call_command("learning_report", "--learn-dry-run", "--user", "alice", stdout=out)
        text = out.getvalue()
        self.assertIn("precision=1.00", text)
        self.assertIn("would write 1", text)
        self.assertEqual(AnswerBank.objects.count(), 0)  # dry run
