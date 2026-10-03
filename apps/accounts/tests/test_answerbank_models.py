from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from apps.accounts.models import AnswerBank, AnswerBankHistory

User = get_user_model()


class AnswerBankModelTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def _row(self, **overrides):
        data = dict(
            profile=self.profile,
            question_key="years of python experience",
            question_text="Years of Python experience?",
            value="7",
            risk_tier=AnswerBank.RiskTier.T2_FACTUAL,
            source=AnswerBank.Source.USER,
        )
        data.update(overrides)
        return AnswerBank.objects.create(**data)

    def test_defaults(self):
        row = self._row()
        self.assertEqual(row.category, AnswerBank.Category.OTHER)
        self.assertEqual(row.confidence, 1.0)
        self.assertFalse(row.is_locked)
        self.assertIsNone(row.expires_at)
        self.assertEqual(row.source_detail, {})

    def test_one_row_per_profile_and_question_key(self):
        self._row()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(value="8")

    def test_same_question_key_allowed_for_different_profiles(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        self._row()
        self._row(profile=other)
        self.assertEqual(AnswerBank.objects.count(), 2)

    def test_database_rejects_a_locked_learned_or_imported_row(self):
        for source in (AnswerBank.Source.LEARNED, AnswerBank.Source.IMPORTED):
            with self.subTest(source=source):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._row(question_key=f"k-{source}", source=source, is_locked=True)

    def test_user_row_may_be_locked(self):
        self.assertTrue(self._row(is_locked=True).is_locked)

    def test_risk_tier_values_match_the_tiering_module(self):
        from apps.accounts.tiering import Tier

        self.assertEqual(
            {c.value for c in AnswerBank.RiskTier},
            {Tier.T0_LEGAL, Tier.T1_COMMERCIAL, Tier.T2_FACTUAL},
        )

    def test_rows_are_removed_with_the_profile(self):
        self._row()
        self.profile.user.delete()
        self.assertEqual(AnswerBank.objects.count(), 0)


class AnswerBankHistoryTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def _history(self):
        return AnswerBankHistory.objects.create(
            profile=self.profile,
            question_key="k",
            value="old",
            risk_tier="t2_factual",
            source="learned",
        )

    def test_history_cannot_be_updated(self):
        entry = self._history()
        entry.value = "tampered"
        with self.assertRaises(ValueError):
            entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.value, "old")

    def test_admin_exposes_history_read_only(self):
        model_admin = admin.site._registry[AnswerBankHistory]
        request = RequestFactory().get("/")
        entry = self._history()
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, entry))
        self.assertFalse(model_admin.has_delete_permission(request, entry))


class ProfileFieldProvenanceFieldTests(TestCase):
    def test_defaults_to_empty_dict(self):
        profile = User.objects.create_user(username="alice", password="pw").profile
        self.assertEqual(profile.field_provenance, {})
