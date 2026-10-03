import ast
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import AnswerBank, AnswerBankHistory
from apps.accounts.services import answer_resolver
from apps.accounts.services.answer_resolver import (
    normalize_question_key,
    resolve_answer,
    write_answer,
)
from apps.accounts.tiering import Tier

User = get_user_model()

PYTHON_Q = "How many years of Python experience do you have?"
SPONSOR_Q = "Will you now or in the future require visa sponsorship?"
SALARY_Q = "What is your desired salary?"


class NormalizeQuestionKeyTests(SimpleTestCase):
    def test_case_whitespace_and_punctuation_collapse(self):
        self.assertEqual(
            normalize_question_key("  Years of  Python\texperience?! "),
            normalize_question_key("years of python experience"),
        )

    def test_option_set_changes_the_key_but_not_its_order(self):
        base = normalize_question_key("Pick one")
        a = normalize_question_key("Pick one", ["Yes", "No"])
        self.assertNotEqual(a, base)
        self.assertEqual(a, normalize_question_key("Pick one", ["No", "Yes"]))
        self.assertNotEqual(a, normalize_question_key("Pick one", ["Yes", "No", "Maybe"]))

    def test_long_questions_fit_the_column_and_stay_distinct(self):
        one = normalize_question_key("word " * 200 + "alpha")
        two = normalize_question_key("word " * 200 + "beta")
        self.assertLessEqual(len(one), 255)
        self.assertNotEqual(one, two)


class _Base(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile


class ResolveAnswerTests(_Base):
    def _store(self, text, value, source, **kwargs):
        return write_answer(self.profile, text, value, source, **kwargs)

    def test_nothing_known_returns_none(self):
        self.assertIsNone(resolve_answer(self.profile, PYTHON_Q))

    def test_user_answer_resolves_with_provenance_and_no_confirmation(self):
        self._store(PYTHON_Q, "7", "user", is_locked=True)
        resolved = resolve_answer(self.profile, "how many years of python experience do you have")
        self.assertEqual(resolved.value, "7")
        self.assertEqual(resolved.provenance["origin"], "answer_bank")
        self.assertEqual(resolved.provenance["source"], "user")
        self.assertTrue(resolved.provenance["locked"])
        self.assertFalse(resolved.needs_confirmation)

    def test_learned_t2_value_needs_no_confirmation(self):
        self._store(PYTHON_Q, "7", "learned")
        self.assertFalse(resolve_answer(self.profile, PYTHON_Q).needs_confirmation)

    def test_learned_and_imported_t0_t1_values_need_confirmation(self):
        for text, source in ((SPONSOR_Q, "learned"), (SALARY_Q, "imported")):
            with self.subTest(text=text, source=source):
                self._store(text, "x", source)
                resolved = resolve_answer(self.profile, text)
                self.assertTrue(resolved.needs_confirmation)

    def test_user_confirmed_t0_value_does_not_need_confirmation(self):
        self._store(SPONSOR_Q, "No", "user")
        resolved = resolve_answer(self.profile, SPONSOR_Q)
        self.assertEqual(resolved.tier, Tier.T0_LEGAL)
        self.assertFalse(resolved.needs_confirmation)

    def test_computed_tier_beats_a_stale_stored_tier(self):
        row = self._store(SPONSOR_Q, "No", "learned").row
        AnswerBank.objects.filter(pk=row.pk).update(risk_tier=Tier.T2_FACTUAL)
        resolved = resolve_answer(self.profile, SPONSOR_Q)
        self.assertEqual(resolved.tier, Tier.T0_LEGAL)
        self.assertTrue(resolved.needs_confirmation)

    def test_expired_learned_and_imported_t2_rows_resolve_to_none(self):
        now = timezone.now()
        for source, age in (("learned", 181), ("imported", 366)):
            with self.subTest(source=source):
                AnswerBank.objects.all().delete()
                self._store(PYTHON_Q, "7", source)
                fresh = resolve_answer(self.profile, PYTHON_Q, now=now + timedelta(days=age - 2))
                stale = resolve_answer(self.profile, PYTHON_Q, now=now + timedelta(days=age))
                self.assertIsNotNone(fresh)
                self.assertIsNone(stale)

    def test_user_rows_and_t0_rows_never_expire_by_age(self):
        self._store(PYTHON_Q, "7", "user")
        self._store(SPONSOR_Q, "No", "learned")
        later = timezone.now() + timedelta(days=2000)
        self.assertIsNotNone(resolve_answer(self.profile, PYTHON_Q, now=later))
        self.assertIsNotNone(resolve_answer(self.profile, SPONSOR_Q, now=later))

    def test_explicit_expires_at_wins(self):
        row = self._store(PYTHON_Q, "7", "user").row
        AnswerBank.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertIsNone(resolve_answer(self.profile, PYTHON_Q))

    def test_other_profiles_rows_are_invisible(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        write_answer(other, PYTHON_Q, "9", "user")
        self.assertIsNone(resolve_answer(self.profile, PYTHON_Q))


class LegacyFallbackTests(_Base):
    def test_legacy_is_used_only_when_answer_bank_misses(self):
        calls = []

        def legacy(text):
            calls.append(text)
            return "legacy"

        resolved = resolve_answer(self.profile, SPONSOR_Q, legacy_lookup=legacy)
        self.assertEqual(resolved.value, "legacy")
        self.assertEqual(resolved.provenance["origin"], "legacy_explicit_answer")
        self.assertFalse(resolved.needs_confirmation)

        write_answer(self.profile, SPONSOR_Q, "bank", "user")
        calls.clear()
        resolved = resolve_answer(self.profile, SPONSOR_Q, legacy_lookup=legacy)
        self.assertEqual(resolved.value, "bank")
        self.assertEqual(calls, [])

    def test_expired_row_falls_through_to_legacy(self):
        row = write_answer(self.profile, PYTHON_Q, "7", "user").row
        AnswerBank.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
        resolved = resolve_answer(self.profile, PYTHON_Q, legacy_lookup=lambda t: "legacy")
        self.assertEqual(resolved.value, "legacy")

    def test_a_hit_is_logged_and_a_miss_is_not(self):
        with self.assertLogs(answer_resolver.logger, "INFO") as logs:
            resolve_answer(self.profile, SPONSOR_Q, legacy_lookup=lambda t: "x")
        self.assertIn("legacy_fallback_hit", logs.output[0])
        with self.assertNoLogs(answer_resolver.logger, "INFO"):
            resolve_answer(self.profile, SPONSOR_Q, legacy_lookup=lambda t: None)

    def test_resolver_module_does_not_import_auto_apply(self):
        tree = ast.parse(Path(answer_resolver.__file__).read_text())
        modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse([m for m in modules if m.startswith("apps.auto_apply")])


class WriteAnswerPrecedenceTests(_Base):
    def test_lower_precedence_cannot_overwrite_higher(self):
        write_answer(self.profile, PYTHON_Q, "user", "user", is_locked=True)
        for source in ("learned", "imported"):
            result = write_answer(self.profile, PYTHON_Q, "auto", source)
            self.assertFalse(result.applied)
        self.assertEqual(resolve_answer(self.profile, PYTHON_Q).value, "user")
        self.assertEqual(AnswerBankHistory.objects.count(), 0)

    def test_imported_cannot_overwrite_learned_but_learned_can_overwrite_imported(self):
        write_answer(self.profile, PYTHON_Q, "i1", "imported")
        self.assertTrue(write_answer(self.profile, PYTHON_Q, "l1", "learned").applied)
        self.assertFalse(write_answer(self.profile, PYTHON_Q, "i2", "imported").applied)
        self.assertEqual(resolve_answer(self.profile, PYTHON_Q).value, "l1")

    def test_user_may_overwrite_anything_including_a_lock(self):
        write_answer(self.profile, PYTHON_Q, "old", "user", is_locked=True)
        self.assertTrue(write_answer(self.profile, PYTHON_Q, "new", "user").applied)
        row = AnswerBank.objects.get()
        self.assertEqual((row.value, row.is_locked), ("new", False))

    def test_replacing_a_value_records_history(self):
        write_answer(self.profile, PYTHON_Q, "old", "learned")
        write_answer(self.profile, PYTHON_Q, "new", "user")
        entry = AnswerBankHistory.objects.get()
        self.assertEqual((entry.value, entry.source, entry.superseded_by_source), ("old", "learned", "user"))

    def test_automation_cannot_lock(self):
        with self.assertRaises(ValueError):
            write_answer(self.profile, PYTHON_Q, "x", "learned", is_locked=True)

    def test_tier_is_computed_on_write_and_never_downgraded(self):
        row = write_answer(self.profile, SPONSOR_Q, "No", "learned").row
        self.assertEqual(row.risk_tier, Tier.T0_LEGAL)
        AnswerBank.objects.filter(pk=row.pk).update(risk_tier=Tier.T0_LEGAL)
        write_answer(self.profile, SPONSOR_Q, "Yes", "user")
        self.assertEqual(AnswerBank.objects.get().risk_tier, Tier.T0_LEGAL)
