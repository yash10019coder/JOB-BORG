import ast
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import AnswerBank, AnswerBankHistory
from apps.accounts.services import answer_resolver
from apps.accounts.services.answer_resolver import (
    load_bank_rows,
    normalize_question_key,
    options_hash,
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

    def test_options_are_not_part_of_the_key(self):
        self.assertEqual(
            normalize_question_key("Pick one"), normalize_question_key("Pick one")
        )
        self.assertEqual(options_hash(["Yes", "No"]), options_hash(["No", "Yes"]))
        self.assertNotEqual(options_hash(["Yes", "No"]), options_hash(["Yes", "No", "Maybe"]))
        self.assertEqual(options_hash([]), "")

    def test_decoration_does_not_change_the_key(self):
        base = normalize_question_key("Website")
        for decorated in ("Website *", "Website (optional)", "Website (Required)", "Website:", "WEBSITE?"):
            self.assertEqual(normalize_question_key(decorated), base, decorated)
        self.assertEqual(
            normalize_question_key("Please describe your role"),
            normalize_question_key("Describe your role"),
        )

    def test_employer_name_is_stripped_when_given(self):
        a = normalize_question_key("Are you willing to relocate to work at Acme?", "Acme")
        b = normalize_question_key("Are you willing to relocate to work at Beta Corp?", "Beta Corp")
        self.assertEqual(a, b)
        self.assertEqual(
            normalize_question_key("What do you love about Acme's product?", "Acme"),
            normalize_question_key("What do you love about  product?"),
        )

    def test_employer_is_kept_when_not_given(self):
        self.assertNotEqual(
            normalize_question_key("Why do you want to work at Acme?"),
            normalize_question_key("Why do you want to work at Beta?"),
        )

    def test_employer_match_is_whole_word_and_case_insensitive(self):
        self.assertEqual(
            normalize_question_key("Why ACME?", "Acme"), normalize_question_key("Why ?")
        )
        # "Acme" inside another word is not the employer.
        self.assertIn("acmeology", normalize_question_key("Do you know acmeology?", "Acme"))

    def test_different_verbs_are_different_questions(self):
        self.assertNotEqual(
            normalize_question_key("Do you have a US passport?"),
            normalize_question_key("Are you a US passport holder?"),
        )

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

    def test_learned_value_of_every_tier_needs_confirmation(self):
        for text in (PYTHON_Q, SPONSOR_Q, SALARY_Q):
            with self.subTest(text=text):
                self._store(text, "x", "learned")
                self.assertTrue(resolve_answer(self.profile, text).needs_confirmation)

    def test_imported_t2_value_is_still_not_held(self):
        self._store(PYTHON_Q, "7", "imported")
        self.assertFalse(resolve_answer(self.profile, PYTHON_Q).needs_confirmation)

    def test_user_value_of_a_t2_question_is_never_held(self):
        self._store(PYTHON_Q, "7", "user")
        self.assertFalse(resolve_answer(self.profile, PYTHON_Q).needs_confirmation)

    def test_learned_rows_are_ignored_while_learning_is_off(self):
        self._store(PYTHON_Q, "7", "learned")
        self.profile.learning_enabled = False
        self.profile.save(update_fields=["learning_enabled"])
        self.assertIsNone(resolve_answer(self.profile, PYTHON_Q))

    def test_user_rows_still_resolve_while_learning_is_off(self):
        self._store(PYTHON_Q, "7", "user")
        self.profile.learning_enabled = False
        self.profile.save(update_fields=["learning_enabled"])
        self.assertEqual(resolve_answer(self.profile, PYTHON_Q).value, "7")

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
    """The legacy rows are the last resort; the module boundary is checked here."""

    def test_a_bank_row_beats_a_legacy_row(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "No", answer_bool=False)
        write_answer(self.profile, SPONSOR_Q, "bank", "user")
        resolved = resolve_answer(self.profile, SPONSOR_Q, _job("US"))
        self.assertEqual((resolved.value, resolved.provenance["origin"]), ("bank", "answer_bank"))

    def test_expired_bank_row_falls_through_to_the_legacy_row(self):
        row = write_answer(self.profile, SPONSOR_Q, "bank", "user").row
        AnswerBank.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "No", answer_bool=False)
        resolved = resolve_answer(self.profile, SPONSOR_Q, _job("US"), options=("Yes", "No"))
        self.assertEqual(
            (resolved.value, resolved.provenance["origin"]), ("No", "legacy_explicit_answer")
        )

    def test_resolver_module_does_not_import_auto_apply(self):
        tree = ast.parse(Path(answer_resolver.__file__).read_text())
        modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse([m for m in modules if m.startswith("apps.auto_apply")])

    def test_typed_layers_do_not_import_auto_apply_or_web(self):
        from apps.accounts import question_semantics
        from apps.accounts.services import typed_facts

        for module in (typed_facts, question_semantics, answer_resolver):
            tree = ast.parse(Path(module.__file__).read_text())
            modules = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            offenders = [m for m in modules if m.startswith(("apps.auto_apply", "apps.web"))]
            self.assertEqual(offenders, [], module.__name__)


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


    def test_expired_row_does_not_block_a_lower_precedence_writer(self):
        write_answer(self.profile, PYTHON_Q, "stale", "learned")
        AnswerBank.objects.update(updated_at=timezone.now() - timedelta(days=200))
        result = write_answer(self.profile, PYTHON_Q, "fresh", "imported")
        self.assertTrue(result.applied)
        self.assertEqual(AnswerBank.objects.get().value, "fresh")
        self.assertEqual(AnswerBankHistory.objects.get().value, "stale")

    def test_unexpired_row_still_blocks_a_lower_precedence_writer(self):
        write_answer(self.profile, PYTHON_Q, "kept", "learned")
        self.assertFalse(write_answer(self.profile, PYTHON_Q, "no", "imported").applied)

    def test_concurrent_first_create_falls_back_to_an_update(self):
        write_answer(self.profile, PYTHON_Q, "winner", "learned")
        real = answer_resolver._locked_row
        calls = []

        def racing(profile, key, scope_region=""):
            # The first read happens "before" the other writer committed.
            calls.append(key)
            return None if len(calls) == 1 else real(profile, key, scope_region)

        with mock.patch.object(answer_resolver, "_locked_row", side_effect=racing):
            result = write_answer(self.profile, PYTHON_Q, "late", "user")
        self.assertTrue(result.applied)
        self.assertEqual(AnswerBank.objects.count(), 1)
        self.assertEqual(AnswerBank.objects.get().value, "late")
        self.assertEqual(AnswerBankHistory.objects.get().value, "winner")

    def test_update_without_a_category_keeps_the_existing_category(self):
        write_answer(
            self.profile, PYTHON_Q, "a", "learned", category=AnswerBank.Category.EXPERIENCE
        )
        write_answer(self.profile, PYTHON_Q, "b", "user")
        self.assertEqual(AnswerBank.objects.get().category, AnswerBank.Category.EXPERIENCE)
        write_answer(
            self.profile, PYTHON_Q, "c", "user", category=AnswerBank.Category.TECHNOLOGIES
        )
        self.assertEqual(AnswerBank.objects.get().category, AnswerBank.Category.TECHNOLOGIES)

    def test_create_without_a_category_defaults_to_other(self):
        write_answer(self.profile, PYTHON_Q, "a", "user")
        self.assertEqual(AnswerBank.objects.get().category, AnswerBank.Category.OTHER)


class PreloadedBankRowsTests(_Base):
    def test_preloaded_rows_resolve_without_a_query(self):
        write_answer(self.profile, PYTHON_Q, "5", "user")
        rows = load_bank_rows(self.profile)
        with self.assertNumQueries(0):
            found = resolve_answer(self.profile, PYTHON_Q, bank_rows=rows)
        self.assertEqual(found.value, "5")

    def test_preloaded_miss_is_a_miss(self):
        with self.assertNumQueries(0):
            self.assertIsNone(resolve_answer(self.profile, PYTHON_Q, bank_rows={}))

    def test_load_bank_rows_is_scoped_to_the_profile(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        write_answer(other, PYTHON_Q, "x", "user")
        self.assertEqual(load_bank_rows(self.profile), {})
        self.assertEqual(load_bank_rows(None), {})


class WriteAnswerScopeTests(_Base):
    RELOCATE_Q = "Are you willing to relocate?"

    def test_same_question_in_different_regions_coexists(self):
        write_answer(self.profile, self.RELOCATE_Q, "Yes", "user", scope_region="US")
        write_answer(self.profile, self.RELOCATE_Q, "No", "user", scope_region="IN")
        write_answer(self.profile, self.RELOCATE_Q, "Maybe", "user")
        rows = {r.scope_region: r.value for r in AnswerBank.objects.all()}
        self.assertEqual(rows, {"US": "Yes", "IN": "No", "": "Maybe"})

    def test_rewriting_one_region_leaves_the_others(self):
        write_answer(self.profile, self.RELOCATE_Q, "Yes", "user", scope_region="US")
        write_answer(self.profile, self.RELOCATE_Q, "No", "user", scope_region="IN")
        write_answer(self.profile, self.RELOCATE_Q, "Yes!", "user", scope_region="US")
        self.assertEqual(AnswerBank.objects.get(scope_region="IN").value, "No")
        entry = AnswerBankHistory.objects.get()
        self.assertEqual((entry.scope_region, entry.value), ("US", "Yes"))

    def test_precedence_is_checked_per_scope(self):
        write_answer(self.profile, self.RELOCATE_Q, "Yes", "user", scope_region="US", is_locked=True)
        self.assertTrue(write_answer(self.profile, self.RELOCATE_Q, "x", "learned", scope_region="IN").applied)
        self.assertFalse(write_answer(self.profile, self.RELOCATE_Q, "x", "learned", scope_region="US").applied)

    def test_duplicate_profile_key_scope_is_rejected_by_the_database(self):
        from django.db import IntegrityError, transaction

        write_answer(self.profile, PYTHON_Q, "5", "user")
        row = AnswerBank.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnswerBank.objects.create(
                profile=self.profile, question_key=row.question_key, scope_region="",
                value="6", risk_tier=row.risk_tier, source="user",
            )

    def test_min_tier_can_raise_but_never_lower(self):
        row = write_answer(self.profile, PYTHON_Q, "5", "user", min_tier=Tier.T1_COMMERCIAL).row
        self.assertEqual(row.risk_tier, Tier.T1_COMMERCIAL)
        row = write_answer(self.profile, SPONSOR_Q, "No", "user", min_tier=Tier.T2_FACTUAL).row
        self.assertEqual(row.risk_tier, Tier.T0_LEGAL)

    def test_applies_everywhere_and_options_hash_are_recorded(self):
        row = write_answer(
            self.profile, self.RELOCATE_Q, "Yes", "user",
            options=["Yes", "No"], applies_everywhere=True,
        ).row
        self.assertEqual(row.source_detail["applies_everywhere"], True)
        self.assertEqual(row.source_detail["options_hash"], options_hash(["Yes", "No"]))

    def test_employer_is_stripped_from_the_stored_key(self):
        question = "What excites you about working at {}?"
        write_answer(self.profile, question.format("Acme"), "The mission", "user", employer="Acme")

        class _Job:
            class employer:
                name = "Beta"

        found = resolve_answer(self.profile, question.format("Beta"), _Job)
        self.assertEqual(found.value, "The mission")
        # Without the job's employer the names differ, so it is a different question.
        self.assertIsNone(resolve_answer(self.profile, question.format("Beta")))


class KeyMigrationTests(_Base):
    def _run(self):
        import importlib

        from django.apps import apps

        module = importlib.import_module(
            "apps.accounts.migrations.0012_answerbank_scope_observation"
        )
        module.strip_options_hash_from_keys(apps, None)

    def _row(self, key, **kwargs):
        defaults = dict(
            profile=self.profile, question_key=key, value="v", risk_tier="t2_factual",
            source="user",
        )
        defaults.update(kwargs)
        return AnswerBank.objects.create(**defaults)

    def test_options_hash_suffix_is_stripped(self):
        self._row("pick one#0123456789")
        self._row("plain key")
        self._run()
        self.assertEqual(
            sorted(AnswerBank.objects.values_list("question_key", flat=True)),
            ["pick one", "plain key"],
        )

    def test_rows_that_collide_keep_the_highest_precedence_and_archive_the_rest(self):
        self._row("pick one#aaaaaaaaaa", source="imported", value="imported")
        self._row("pick one#bbbbbbbbbb", source="learned", value="learned")
        self._row("pick one#cccccccccc", source="user", value="user", is_locked=True)
        self._run()
        row = AnswerBank.objects.get()
        self.assertEqual((row.question_key, row.value), ("pick one", "user"))
        self.assertEqual(
            sorted(AnswerBankHistory.objects.values_list("value", flat=True)),
            ["imported", "learned"],
        )
        self.assertTrue(
            all(h.superseded_by_source == "migration" for h in AnswerBankHistory.objects.all())
        )

    def test_history_keys_are_stripped_too(self):
        AnswerBankHistory.objects.create(
            profile=self.profile, question_key="pick one#0123456789", value="old",
            risk_tier="t2_factual", source="user",
        )
        self._run()
        self.assertEqual(AnswerBankHistory.objects.get().question_key, "pick one")

    def test_a_hash_that_is_not_ten_hex_characters_is_left_alone(self):
        self._row("c# developer")
        self._run()
        self.assertEqual(AnswerBank.objects.get().question_key, "c# developer")

    def test_other_profiles_are_not_merged(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        self._row("pick one#aaaaaaaaaa")
        self._row("pick one#bbbbbbbbbb", profile=other)
        self._run()
        self.assertEqual(AnswerBank.objects.count(), 2)


class AnswerObservationTests(_Base):
    def _make(self, **kwargs):
        from apps.accounts.models import AnswerObservation

        return AnswerObservation.objects.create(
            profile=self.profile, question_key="how did you hear about us",
            value="LinkedIn", tier="t2_factual", **kwargs,
        )

    def test_is_append_only(self):
        observation = self._make()
        observation.value = "Friend"
        with self.assertRaises(ValueError):
            observation.save()

    def test_is_deleted_with_the_profile(self):
        from apps.accounts.models import AnswerObservation

        self._make(job_id=7, draft_id=9, employer_name="Acme", job_region="US")
        self.profile.user.delete()
        self.assertEqual(AnswerObservation.objects.count(), 0)

    def test_admin_cannot_add_change_or_delete(self):
        from django.contrib import admin
        from apps.accounts.models import AnswerObservation

        model_admin = admin.site._registry[AnswerObservation]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))


def _job(country="US", employer="Acme"):
    from types import SimpleNamespace

    return SimpleNamespace(location_country=country, employer=SimpleNamespace(name=employer))


class TypedFactsFirstTests(_Base):
    AUTH_Q = "Are you legally authorized to work in the United States?"

    def test_a_typed_fact_beats_a_user_answer_bank_row(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        write_answer(self.profile, self.AUTH_Q, "No", "user", is_locked=True)
        found = resolve_answer(self.profile, self.AUTH_Q, _job("US"), options=("Yes", "No"))
        self.assertEqual(found.value, "Yes")
        self.assertEqual(found.provenance["origin"], "profile.visa_status_by_country.USA")

    def test_typed_provenance_reports_user_source_and_needs_no_confirmation(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        found = resolve_answer(self.profile, self.AUTH_Q, _job("US"), options=("Yes", "No"))
        self.assertEqual(found.provenance["source"], "user")
        self.assertFalse(found.needs_confirmation)
        self.assertEqual(found.tier, Tier.T0_LEGAL)

    def test_an_imported_typed_value_in_a_t0_question_needs_confirmation(self):
        from apps.accounts.services import profile_fields

        profile_fields.apply_profile_field(
            self.profile, "visa_status_by_country.USA", "citizen", "imported"
        )
        found = resolve_answer(self.profile, self.AUTH_Q, _job("US"), options=("Yes", "No"))
        self.assertEqual(found.provenance["source"], "imported")
        self.assertTrue(found.needs_confirmation)

    def test_an_imported_typed_value_in_a_t2_question_does_not(self):
        from apps.accounts.services import profile_fields

        profile_fields.apply_profile_field(self.profile, "location_country", "IND", "imported")
        found = resolve_answer(self.profile, "Country", _job("US"), options=("India +91",))
        self.assertEqual(found.value, "India +91")
        self.assertEqual(found.provenance["source"], "imported")
        self.assertFalse(found.needs_confirmation)

    def test_covered_but_unanswerable_lets_a_user_bank_row_through(self):
        self.profile.visa_status_by_country = {"USA": "h1b"}
        self.profile.save()
        sponsor_q = "Will you now or in the future require visa sponsorship?"
        write_answer(self.profile, sponsor_q, "Yes", "user")
        found = resolve_answer(self.profile, sponsor_q, _job("US"), options=("Yes", "No"))
        self.assertEqual((found.value, found.provenance["origin"]), ("Yes", "answer_bank"))

    def test_covered_but_unanswerable_does_not_let_a_legacy_row_through(self):
        self.profile.visa_status_by_country = {"USA": "h1b"}
        self.profile.save()
        sponsor_q = "Will you now or in the future require visa sponsorship?"
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        self.assertIsNone(resolve_answer(self.profile, sponsor_q, _job("US"), options=("Yes", "No")))

    def test_not_covered_lets_the_legacy_row_answer(self):
        sponsor_q = "Will you now or in the future require visa sponsorship?"
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        found = resolve_answer(self.profile, sponsor_q, _job("US"), options=("Yes", "No"))
        self.assertEqual((found.value, found.provenance["origin"]), ("Yes", "legacy_explicit_answer"))

    def test_no_profile_means_no_typed_facts(self):
        self.assertIsNone(resolve_answer(None, self.AUTH_Q, _job("US")))


def _legacy_row(profile, key, value, **detail):
    detail.setdefault("origin", "legacy_explicit_answer")
    return AnswerBank.objects.create(
        profile=profile, question_key=key, value=value,
        risk_tier="t1_commercial" if key.endswith("salary_expectation") else "t0_legal",
        source="user", is_locked=True, source_detail=detail,
    )


class BankOptionMappingTests(_Base):
    Q = "Are you comfortable with our hiring process?"

    def test_a_stored_yes_fills_whatever_the_form_calls_yes(self):
        write_answer(self.profile, self.Q, "Yes", "user")
        for options, expected in [
            (("Yes", "No"), "Yes"),
            (("Yes, I am", "No, I am not"), "Yes, I am"),
            ((), "Yes"),
        ]:
            self.assertEqual(resolve_answer(self.profile, self.Q, options=options).value, expected)

    def test_a_stored_value_that_fits_no_option_comes_back_unmapped(self):
        write_answer(self.profile, self.Q, "Probably", "user")
        found = resolve_answer(self.profile, self.Q, options=("Yes", "No"))
        self.assertIsNone(found.value)
        self.assertEqual(found.provenance["unmapped_value"], "Probably")

    def test_exact_match_ignores_case_and_punctuation(self):
        write_answer(self.profile, "Pick your stack", "node.js", "user")
        self.assertEqual(
            resolve_answer(self.profile, "Pick your stack", options=("Go", "Node.js")).value,
            "Node.js",
        )

    def test_ambiguous_yes_no_options_are_not_guessed(self):
        write_answer(self.profile, self.Q, "No", "user")
        found = resolve_answer(
            self.profile, self.Q, options=("No, I require sponsorship", "No, but I do not")
        )
        self.assertIsNone(found.value)

    def test_multi_select_values_map_item_by_item(self):
        write_answer(self.profile, "Which languages do you use?", ["python", "go"], "user")
        found = resolve_answer(
            self.profile, "Which languages do you use?", options=("Python", "Go", "Rust")
        )
        self.assertEqual(found.value, ["Python", "Go"])
        none = resolve_answer(
            self.profile, "Which languages do you use?", options=("Python", "Rust")
        )
        self.assertIsNone(none.value)

    def test_the_same_answer_is_found_whatever_the_option_set(self):
        write_answer(self.profile, self.Q, "Yes", "user", options=("Yes", "No"))
        self.assertEqual(
            resolve_answer(self.profile, self.Q, options=("Yes", "No", "Maybe")).value, "Yes"
        )


class RegionScopedResolveTests(_Base):
    Q = "Are you willing to relocate?"

    def test_a_us_answer_is_used_for_a_us_job_only(self):
        write_answer(self.profile, self.Q, "Yes", "user", scope_region="US")
        self.assertEqual(resolve_answer(self.profile, self.Q, _job("US")).value, "Yes")
        self.assertIsNone(resolve_answer(self.profile, self.Q, _job("India")))

    def test_an_unknown_job_region_resolves_nothing_for_a_scoped_answer(self):
        write_answer(self.profile, self.Q, "Yes", "user", scope_region="US")
        self.assertIsNone(resolve_answer(self.profile, self.Q, _job("")))
        self.assertIsNone(resolve_answer(self.profile, self.Q))

    def test_each_region_gets_its_own_answer(self):
        write_answer(self.profile, self.Q, "Yes", "user", scope_region="US")
        write_answer(self.profile, self.Q, "No", "user", scope_region="IN")
        self.assertEqual(resolve_answer(self.profile, self.Q, _job("US")).value, "Yes")
        self.assertEqual(resolve_answer(self.profile, self.Q, _job("India")).value, "No")

    def test_a_global_row_is_ignored_for_location_sensitive_questions_unless_marked(self):
        write_answer(self.profile, self.Q, "Yes", "user")
        self.assertIsNone(resolve_answer(self.profile, self.Q, _job("US")))

    def test_applies_everywhere_row_is_used_for_any_job(self):
        write_answer(self.profile, self.Q, "Yes", "user", applies_everywhere=True)
        for country in ("US", "India", ""):
            self.assertEqual(resolve_answer(self.profile, self.Q, _job(country)).value, "Yes")

    def test_a_regional_row_beats_an_applies_everywhere_row(self):
        write_answer(self.profile, self.Q, "Maybe", "user", applies_everywhere=True)
        write_answer(self.profile, self.Q, "No", "user", scope_region="IN")
        self.assertEqual(resolve_answer(self.profile, self.Q, _job("India")).value, "No")
        self.assertEqual(resolve_answer(self.profile, self.Q, _job("US")).value, "Maybe")

    def test_a_non_location_question_uses_the_global_row_for_every_job(self):
        write_answer(self.profile, PYTHON_Q, "5", "user")
        for country in ("US", "India", ""):
            self.assertEqual(resolve_answer(self.profile, PYTHON_Q, _job(country)).value, "5")

    def test_a_named_country_is_part_of_the_key_not_a_scope(self):
        us = "Are you legally authorized to work in the United States?"
        write_answer(self.profile, us, "Yes", "user")
        self.assertEqual(resolve_answer(self.profile, us, _job("India")).value, "Yes")

    def test_preloaded_rows_respect_scope(self):
        write_answer(self.profile, self.Q, "Yes", "user", scope_region="US")
        rows = load_bank_rows(self.profile)
        with self.assertNumQueries(0):
            self.assertEqual(
                resolve_answer(self.profile, self.Q, _job("US"), bank_rows=rows).value, "Yes"
            )
            self.assertIsNone(resolve_answer(self.profile, self.Q, _job("India"), bank_rows=rows))


class LegacyRowTests(_Base):
    AUTH_Q = "Are you legally authorized to work in the United States?"
    SPONSOR_Q = "Will you now or in the future require visa sponsorship?"
    SALARY_Q = "What is your desired salary?"

    def test_boolean_rows_map_onto_the_forms_yes_no(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_WORK_AUTHORIZATION, "No", answer_bool=False)
        for options, expected in [(("Yes", "No"), "No"), (("Yes, I am", "No, I am not"), "No, I am not")]:
            found = resolve_answer(self.profile, self.AUTH_Q, _job("US"), options=options)
            self.assertEqual(found.value, expected)
            self.assertEqual(found.provenance["origin"], "legacy_explicit_answer")
            self.assertFalse(found.needs_confirmation)
            self.assertEqual(found.tier, Tier.T0_LEGAL)

    def test_authorization_and_sponsorship_rows_are_not_interchangeable(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        self.assertIsNone(resolve_answer(self.profile, self.AUTH_Q, _job("US")))

    def test_ambiguous_and_negated_questions_use_no_legacy_row(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        for text in (
            "What is your current immigration status?",
            "Can you work in the US without sponsorship?",
            "Are you authorized to work in the US, and will you require sponsorship?",
        ):
            self.assertIsNone(resolve_answer(self.profile, text, _job("US")), text)

    def test_an_other_answer_never_resolves(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Other", answer_bool=None)
        self.assertIsNone(resolve_answer(self.profile, self.SPONSOR_Q, _job("US")))

    def test_free_text_rows_keep_the_phase_one_behaviour(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Not at the moment", free_text=True)
        self.assertEqual(
            resolve_answer(self.profile, self.SPONSOR_Q, _job("US")).value, "Not at the moment"
        )
        # Free text that fits none of the form's options is not guessed: the
        # answer exists but comes back unmapped (blank for review).
        unmapped = resolve_answer(self.profile, self.SPONSOR_Q, _job("US"), options=("Yes", "No"))
        self.assertIsNone(unmapped.value)
        self.assertEqual(unmapped.provenance["unmapped_value"], "Not at the moment")

    def test_a_salary_row_answers_only_a_job_in_its_own_region(self):
        _legacy_row(
            self.profile, answer_resolver.LEGACY_SALARY, "$100,000 - $125,000", region="US"
        )
        self.assertEqual(
            resolve_answer(self.profile, self.SALARY_Q, _job("US")).value, "$100,000 - $125,000"
        )
        self.assertIsNone(resolve_answer(self.profile, self.SALARY_Q, _job("India")))
        self.assertIsNone(resolve_answer(self.profile, self.SALARY_Q, _job("")))
        self.assertIsNone(resolve_answer(self.profile, self.SALARY_Q))

    def test_a_salary_row_without_a_region_never_answers(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SALARY, "$100,000 - $125,000", region=None)
        self.assertIsNone(resolve_answer(self.profile, self.SALARY_Q, _job("US")))

    def test_a_hit_is_logged_and_a_miss_is_not(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        with self.assertLogs("apps.accounts.services.answer_resolver", level="INFO") as logs:
            resolve_answer(self.profile, self.SPONSOR_Q, _job("US"))
        self.assertTrue(any("legacy_fallback_hit" in r for r in logs.output))
        with self.assertNoLogs("apps.accounts.services.answer_resolver", level="INFO"):
            resolve_answer(self.profile, PYTHON_Q, _job("US"))

    def test_an_expired_legacy_row_does_not_answer(self):
        row = _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        AnswerBank.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertIsNone(resolve_answer(self.profile, self.SPONSOR_Q, _job("US")))

    def test_a_disagreeing_typed_fact_wins_and_the_conflict_is_logged_not_blocking(self):
        self.profile.visa_status_by_country = {"USA": "requires_sponsorship"}
        self.profile.save()
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "No", answer_bool=False)
        with self.assertLogs("apps.accounts.services.answer_resolver", level="INFO") as logs:
            found = resolve_answer(
                self.profile, self.SPONSOR_Q, _job("US"), options=("Yes", "No")
            )
        self.assertEqual(found.value, "Yes")
        self.assertFalse(found.needs_confirmation)
        self.assertEqual(found.provenance["conflict"]["answer_bool"], False)
        self.assertTrue(any("legacy_conflict" in r for r in logs.output))

    def test_an_agreeing_legacy_row_is_no_conflict(self):
        self.profile.visa_status_by_country = {"USA": "requires_sponsorship"}
        self.profile.save()
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        found = resolve_answer(self.profile, self.SPONSOR_Q, _job("US"), options=("Yes", "No"))
        self.assertNotIn("conflict", found.provenance)

    def test_the_preloaded_path_reads_legacy_rows_without_a_query(self):
        _legacy_row(self.profile, answer_resolver.LEGACY_SPONSORSHIP, "Yes", answer_bool=True)
        rows = load_bank_rows(self.profile)
        with self.assertNumQueries(0):
            found = resolve_answer(
                self.profile, self.SPONSOR_Q, _job("US"), options=("Yes", "No"), bank_rows=rows
            )
        self.assertEqual(found.value, "Yes")
