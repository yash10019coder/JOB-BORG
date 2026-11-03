import importlib
from types import SimpleNamespace

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import AnswerBank, Profile
from apps.accounts.services.answer_resolver import resolve_answer, write_answer
from apps.auto_apply.models import ExplicitAnswer

User = get_user_model()

migration = importlib.import_module("apps.auto_apply.migrations.0011_backfill_explicit_answers")
Category = ExplicitAnswer.Category


def _job(country="US"):
    return SimpleNamespace(location_country=country, employer=SimpleNamespace(name="Acme"))


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = self.user.profile

    def give(self, category, text, user=None):
        return ExplicitAnswer.objects.create(
            user=user or self.user, category=category, answer_text=text
        )

    def run_backfill(self, **kwargs):
        migration.backfill_explicit_answers(apps, None, **kwargs)

    def row(self, key, profile=None):
        return AnswerBank.objects.get(profile=profile or self.profile, question_key=key)


class BackfillMappingTests(_Base):
    def test_the_dev_shaped_trio_becomes_three_locked_user_rows(self):
        self.give(Category.WORK_AUTHORIZATION, "no_need_sponsorship")
        self.give(Category.SPONSORSHIP, "other")
        self.give(Category.SALARY_EXPECTATION, "100-125k")

        self.run_backfill()

        self.assertEqual(AnswerBank.objects.count(), 3)
        for row in AnswerBank.objects.all():
            self.assertEqual((row.source, row.is_locked, row.scope_region), ("user", True, ""))
            self.assertEqual(row.source_detail["origin"], "legacy_explicit_answer")
            self.assertIn("explicit_answer_id", row.source_detail)

    def test_work_authorization_codes(self):
        expected = {
            "yes_authorized": True,
            "yes_h1b": True,
            "yes_green_card": True,
            "no_sponsorship_needed": False,
            "no_need_sponsorship": False,
            "other": None,
        }
        for code, boolean in expected.items():
            AnswerBank.objects.all().delete()
            ExplicitAnswer.objects.all().delete()
            self.give(Category.WORK_AUTHORIZATION, code)
            self.run_backfill()
            row = self.row("legacy:work_authorization")
            self.assertEqual(row.source_detail["answer_bool"], boolean, code)
            self.assertEqual(row.source_detail["legacy_code"], code)
            self.assertEqual(row.risk_tier, "t0_legal")

    def test_work_authorization_value_is_the_readable_label(self):
        self.give(Category.WORK_AUTHORIZATION, "no_need_sponsorship")
        self.run_backfill()
        self.assertEqual(self.row("legacy:work_authorization").value, "No, I require visa sponsorship")

    def test_sponsorship_codes(self):
        expected = {"no": False, "yes_h1b": True, "yes_green_card_process": True, "other": None}
        for code, boolean in expected.items():
            AnswerBank.objects.all().delete()
            ExplicitAnswer.objects.all().delete()
            self.give(Category.SPONSORSHIP, code)
            self.run_backfill()
            row = self.row("legacy:sponsorship")
            self.assertEqual(row.source_detail["answer_bool"], boolean, code)
            self.assertEqual(row.risk_tier, "t0_legal")

    def test_a_us_only_salary_band_gets_its_label_and_region(self):
        self.give(Category.SALARY_EXPECTATION, "100-125k")
        self.run_backfill()
        row = self.row("legacy:salary_expectation")
        self.assertEqual(row.value, "$100,000 – $125,000")
        self.assertEqual(row.source_detail["region"], "US")
        self.assertEqual(row.risk_tier, "t1_commercial")

    def test_a_band_shared_by_several_regions_has_no_region(self):
        self.give(Category.SALARY_EXPECTATION, "60-80k")  # UK, CA and SG all use it
        self.run_backfill()
        row = self.row("legacy:salary_expectation")
        self.assertEqual((row.value, row.source_detail["region"]), ("60-80k", None))

    def test_negotiable_and_other_have_no_region(self):
        for code in ("negotiable", "other"):
            AnswerBank.objects.all().delete()
            ExplicitAnswer.objects.all().delete()
            self.give(Category.SALARY_EXPECTATION, code)
            self.run_backfill()
            self.assertIsNone(self.row("legacy:salary_expectation").source_detail["region"], code)

    def test_text_that_is_not_an_old_code_is_kept_verbatim_as_free_text(self):
        self.give(Category.SPONSORSHIP, "Not at the moment")
        self.run_backfill()
        row = self.row("legacy:sponsorship")
        self.assertEqual(row.value, "Not at the moment")
        self.assertTrue(row.source_detail["free_text"])
        self.assertIsNone(row.source_detail["answer_bool"])

    def test_the_other_category_becomes_a_display_only_row(self):
        self.give(Category.OTHER, "I prefer async communication")
        self.run_backfill()
        row = self.row("legacy:other")
        self.assertEqual(row.value, "I prefer async communication")
        self.assertEqual(row.risk_tier, "t2_factual")


class BackfillSafetyTests(_Base):
    def test_running_twice_is_idempotent(self):
        self.give(Category.WORK_AUTHORIZATION, "yes_authorized")
        self.run_backfill()
        self.run_backfill()
        self.assertEqual(AnswerBank.objects.count(), 1)

    def test_an_existing_row_is_never_overwritten(self):
        AnswerBank.objects.create(
            profile=self.profile, question_key="legacy:sponsorship", value="mine",
            risk_tier="t0_legal", source="user", is_locked=True, source_detail={"answer_bool": True},
        )
        self.give(Category.SPONSORSHIP, "no")
        self.run_backfill()
        self.assertEqual(self.row("legacy:sponsorship").value, "mine")

    def test_each_users_rows_land_on_their_own_profile(self):
        bob = User.objects.create_user(username="bob", password="pw")
        self.give(Category.SPONSORSHIP, "no")
        self.give(Category.SPONSORSHIP, "yes_h1b", user=bob)
        self.run_backfill()
        self.assertEqual(self.row("legacy:sponsorship").source_detail["answer_bool"], False)
        self.assertEqual(
            self.row("legacy:sponsorship", bob.profile).source_detail["answer_bool"], True
        )

    def test_user_ids_limits_the_run(self):
        bob = User.objects.create_user(username="bob", password="pw")
        self.give(Category.SPONSORSHIP, "no")
        self.give(Category.SPONSORSHIP, "yes_h1b", user=bob)
        self.run_backfill(user_ids=[bob.pk])
        self.assertEqual(AnswerBank.objects.count(), 1)
        self.assertEqual(AnswerBank.objects.get().profile, bob.profile)

    def test_a_user_without_a_profile_gets_one(self):
        self.give(Category.SPONSORSHIP, "no")
        Profile.objects.filter(user=self.user).delete()
        self.run_backfill()
        self.assertEqual(AnswerBank.objects.get().profile.user_id, self.user.pk)

    def test_the_explicit_answer_table_is_untouched(self):
        self.give(Category.SPONSORSHIP, "no")
        self.run_backfill()
        self.assertEqual(ExplicitAnswer.objects.count(), 1)

    def test_nothing_to_backfill_is_fine(self):
        self.run_backfill()
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_admin_is_read_only(self):
        model_admin = admin.site._registry[ExplicitAnswer]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))


class BackfilledRowsAnswerQuestionsTests(_Base):
    def test_a_backfilled_no_need_sponsorship_answers_authorization_with_no(self):
        self.give(Category.WORK_AUTHORIZATION, "no_need_sponsorship")
        self.run_backfill()
        found = resolve_answer(
            self.profile,
            "Are you legally authorized to work in the United States?",
            _job("US"),
            options=("Yes", "No"),
        )
        self.assertEqual(found.value, "No")
        self.assertEqual(found.provenance["origin"], "legacy_explicit_answer")
        self.assertFalse(found.needs_confirmation)

    def test_the_backfilled_us_salary_answers_a_us_job_only(self):
        self.give(Category.SALARY_EXPECTATION, "100-125k")
        self.run_backfill()
        question = "What is your desired salary?"
        self.assertEqual(
            resolve_answer(self.profile, question, _job("US")).value, "$100,000 – $125,000"
        )
        self.assertIsNone(resolve_answer(self.profile, question, _job("India")))

    def test_a_typed_fact_still_wins_over_the_backfilled_row(self):
        self.give(Category.WORK_AUTHORIZATION, "no_need_sponsorship")
        self.run_backfill()
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        found = resolve_answer(
            self.profile,
            "Are you legally authorized to work in the United States?",
            _job("US"),
            options=("Yes", "No"),
        )
        self.assertEqual(found.value, "Yes")
        self.assertEqual(found.provenance["conflict"]["answer_bool"], False)

    def test_a_user_written_answer_beats_a_legacy_row(self):
        self.give(Category.SPONSORSHIP, "no")
        self.run_backfill()
        question = "Will you now or in the future require visa sponsorship?"
        write_answer(self.profile, question, "Yes", "user")
        self.assertEqual(
            resolve_answer(self.profile, question, _job("US"), options=("Yes", "No")).value, "Yes"
        )
