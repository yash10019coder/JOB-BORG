from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import AnswerObservation, ProfileSuggestion


class LearningModelTests(TestCase):
    def setUp(self):
        self.profile = get_user_model().objects.create_user("u1", "u1@example.com", "pw").profile

    def test_learning_is_on_by_default(self):
        self.assertTrue(self.profile.learning_enabled)

    def _suggestion(self, fingerprint="a" * 40, **extra):
        return ProfileSuggestion.objects.create(
            profile=self.profile,
            question_key="notice period",
            value="2 weeks",
            value_fingerprint=fingerprint,
            tier="t1_commercial",
            **extra,
        )

    def test_suggestion_defaults_to_pending(self):
        self.assertEqual(self._suggestion().status, ProfileSuggestion.Status.PENDING)

    def test_same_value_twice_is_rejected_but_a_new_value_is_allowed(self):
        self._suggestion()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._suggestion()
        self._suggestion(fingerprint="b" * 40)
        self.assertEqual(ProfileSuggestion.objects.count(), 2)

    def test_scope_region_makes_a_distinct_suggestion(self):
        self._suggestion()
        self._suggestion(scope_region="US")
        self.assertEqual(ProfileSuggestion.objects.count(), 2)

    def test_observation_signal_flags_default_off(self):
        obs = AnswerObservation.objects.create(
            profile=self.profile, question_key="q", value="v", tier="t2_factual"
        )
        self.assertFalse(obs.user_confirmed)
        self.assertFalse(obs.user_edited)
        self.assertFalse(obs.remember_declined)
        self.assertIsNone(obs.learned_value)
