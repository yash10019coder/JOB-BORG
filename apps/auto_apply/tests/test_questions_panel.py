from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import AnswerBank
from apps.accounts.services.answer_resolver import delete_answer, write_answer
from apps.accounts.services.panel_cache import panel_cache_key
from apps.auto_apply.greenhouse_form.field_mapping import (
    FILE, SINGLE_SELECT, TEXT, TEXTAREA, FormField, FormSchema, schema_to_dict,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services import questions_panel
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()

HEARD = "How did you hear about us?"
AUTH = "Are you legally authorized to work in the United States?"
ESSAY = "Why do you want to work at Acme?"
GENDER = "Gender"
COUNTRY = "Country"


class PanelTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw", email="a@example.com")
        self.profile = self.user.profile
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = self._job("1")
        cache.clear()

    def _job(self, source_id, country="US"):
        return Job.objects.create(
            source_ats="greenhouse", source_job_id=source_id, employer=self.employer,
            title="Engineer", source_url="https://example.com", location_country=country,
        )

    def draft(self, *fields, job=None, user=None, age_days=0):
        """A finished draft whose form had `fields` (FormField instances)."""
        draft = AutoApplyDraft.objects.create(
            user=user or self.user, job=job or self.job, status=AutoApplyDraft.Status.FAILED,
            form_schema_snapshot=schema_to_dict(FormSchema(fields=tuple(fields))),
        )
        if age_days:
            AutoApplyDraft.objects.filter(pk=draft.pk).update(
                created_at=timezone.now() - timedelta(days=age_days)
            )
        return draft

    def panel(self):
        cache.clear()
        return questions_panel.build_panel(self.user)

    def rows(self, panel=None):
        panel = panel or self.panel()
        return {row["label"]: row | {"group": g["id"]} for g in panel["groups"] for row in g["rows"]}


class AggregationTests(PanelTestCase):
    def test_counts_and_last_seen_across_drafts(self):
        field = FormField(HEARD, TEXT, False, ())
        self.draft(field, job=self._job("a"), age_days=5)
        newest = self.draft(field, job=self._job("b"), age_days=1)
        self.draft(field, job=self._job("c"), age_days=9)
        row = self.rows()[HEARD]
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["last_seen"], AutoApplyDraft.objects.get(pk=newest.pk).created_at)

    def test_the_same_question_across_employers_is_one_row(self):
        other = Employer.objects.create(name="Beta", slug="beta")
        beta_job = Job.objects.create(
            source_ats="greenhouse", source_job_id="b", employer=other, title="x",
            source_url="https://example.com", location_country="US",
        )
        self.draft(FormField("What excites you about working at Acme?", TEXT, False, ()), job=self.job)
        self.draft(FormField("What excites you about working at Beta?", TEXT, False, ()), job=beta_job)
        rows = [r for r in self.rows().values() if "excites" in r["label"]]
        self.assertEqual(sum(r["count"] for r in rows), 2)
        self.assertEqual(len(rows), 1)

    def test_only_the_last_200_drafts_are_used(self):
        for i in range(205):
            self.draft(FormField(f"Unique question number {i}", TEXT, False, ()), age_days=300 - i)
        panel = self.panel()
        self.assertEqual(panel["question_count"], 200)
        labels = set(self.rows(panel))
        self.assertIn("Unique question number 204", labels)
        self.assertNotIn("Unique question number 0", labels)

    def test_file_fields_and_drafts_without_a_snapshot_are_skipped(self):
        self.draft(FormField("Resume/CV", FILE, True, ()))
        AutoApplyDraft.objects.create(
            user=self.user, job=self._job("x"), status=AutoApplyDraft.Status.EXCLUDED,
            form_schema_snapshot=None,
        )
        self.assertEqual(self.panel()["question_count"], 0)

    def test_other_users_drafts_are_never_included(self):
        bob = User.objects.create_user(username="bob", password="pw")
        self.draft(FormField("Bob's private question", TEXT, False, ()), user=bob, job=self._job("z"))
        self.assertEqual(self.panel()["question_count"], 0)

    def test_rows_are_ordered_by_how_often_they_were_asked(self):
        for i in range(3):
            self.draft(FormField(HEARD, TEXT, False, ()), job=self._job(f"h{i}"))
        self.draft(FormField("Rarely asked thing", TEXT, False, ()), job=self._job("r"))
        group = next(g for g in self.panel()["groups"] if g["id"] == "other")
        self.assertEqual([r["label"] for r in group["rows"]], [HEARD, "Rarely asked thing"])

    def test_an_empty_panel(self):
        self.assertEqual(self.panel()["question_count"], 0)


class GroupingAndStatusTests(PanelTestCase):
    def test_settings_backed_questions_never_offer_quick_fill(self):
        self.draft(
            FormField(AUTH, SINGLE_SELECT, True, ("Yes", "No")),
            FormField(COUNTRY, SINGLE_SELECT, True, ("India +91",)),
            FormField("What is your desired salary?", TEXT, True, ()),
            FormField("Are you a US citizen?", SINGLE_SELECT, True, ("Yes", "No")),
        )
        rows = self.rows()
        for label in (AUTH, COUNTRY, "What is your desired salary?", "Are you a US citizen?"):
            self.assertIsNone(rows[label]["quick_fill"], label)
            self.assertFalse(rows[label]["answered"], label)
        self.assertEqual(rows[AUTH]["group"], "work_auth")
        self.assertEqual(rows[COUNTRY]["group"], "contact")
        self.assertEqual(rows["What is your desired salary?"]["group"], "salary")
        self.assertEqual(rows["Are you a US citizen?"]["group"], "citizenship")
        self.assertEqual(rows[AUTH]["anchor"], "work-auth")

    def test_work_authorization_flips_to_answered_once_the_setting_exists(self):
        self.draft(FormField(AUTH, SINGLE_SELECT, True, ("Yes", "No")))
        before = self.rows()[AUTH]
        self.assertIn("not covered", before["status"])
        self.assertIn("no entry for that country", before["status"])
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        after = self.rows()[AUTH]
        self.assertTrue(after["answered"])
        self.assertEqual(after["status"], "answered by your settings")

    def test_country_is_answered_by_the_location_setting(self):
        self.draft(FormField(COUNTRY, SINGLE_SELECT, True, ("United States +1", "India +91")))
        self.assertFalse(self.rows()[COUNTRY]["answered"])
        self.profile.location_country = "IND"
        self.profile.save()
        self.assertTrue(self.rows()[COUNTRY]["answered"])

    def test_standard_fields_are_contact_rows_answered_from_the_profile(self):
        self.draft(
            FormField("First Name", TEXT, True, ()), FormField("Phone", TEXT, True, ()),
        )
        rows = self.rows()
        self.assertEqual(rows["First Name"]["group"], "contact")
        self.assertFalse(rows["Phone"]["answered"])
        self.profile.phone = "555-0100"
        self.profile.full_name = "Alice Smith"
        self.profile.save()
        rows = self.rows()
        self.assertTrue(rows["Phone"]["answered"])
        self.assertTrue(rows["First Name"]["answered"])

    def test_a_saved_answer_marks_a_question_answered_with_its_source(self):
        self.draft(FormField(HEARD, TEXT, False, ()))
        self.assertEqual(self.rows()[HEARD]["status"], "not answered yet")
        write_answer(self.profile, HEARD, "LinkedIn", "user")
        row = self.rows()[HEARD]
        self.assertTrue(row["answered"])
        self.assertEqual(row["source"], "user")

    def test_an_unanswered_question_offers_quick_fill_pointing_at_its_draft_and_field(self):
        draft = self.draft(FormField("Name", TEXT, True, ()), FormField(HEARD, TEXT, False, ()))
        row = self.rows()[HEARD]
        self.assertEqual(row["quick_fill"], {"draft": draft.pk, "field": 1})

    def test_long_text_offers_no_quick_fill(self):
        self.draft(FormField(ESSAY, TEXTAREA, True, ()))
        row = self.rows()[ESSAY]
        self.assertIsNone(row["quick_fill"])
        self.assertIn("specific to one employer", row["status"])

    def test_demographic_and_legal_questions_get_their_own_groups(self):
        self.draft(
            FormField(GENDER, SINGLE_SELECT, False, ("Male", "Female")),
            FormField("I certify under penalty of perjury that this is true", TEXT, True, ()),
        )
        rows = self.rows()
        self.assertEqual(rows[GENDER]["group"], "eeo")
        self.assertEqual(
            rows["I certify under penalty of perjury that this is true"]["group"], "legal"
        )

    def test_a_stored_answer_that_does_not_fit_the_options_is_not_counted_answered(self):
        self.draft(FormField(HEARD, SINGLE_SELECT, False, ("Friend", "Ad")))
        write_answer(self.profile, HEARD, "LinkedIn", "user")
        self.assertFalse(self.rows()[HEARD]["answered"])

    def test_group_totals(self):
        self.draft(FormField(HEARD, TEXT, False, ()), FormField("Pick a color", TEXT, False, ()))
        write_answer(self.profile, HEARD, "LinkedIn", "user")
        other = next(g for g in self.panel()["groups"] if g["id"] == "other")
        self.assertEqual((other["answered"], other["total"]), (1, 2))


class CacheTests(PanelTestCase):
    def test_a_second_read_is_served_from_the_cache_without_queries(self):
        self.draft(FormField(HEARD, TEXT, False, ()))
        first = questions_panel.get_panel(self.user)
        with self.assertNumQueries(0):
            second = questions_panel.get_panel(self.user)
        self.assertEqual(first, second)

    def test_building_does_a_constant_number_of_queries(self):
        for i in range(30):
            self.draft(
                FormField(f"Question {i}", TEXT, False, ()), FormField(f"Other {i}", TEXT, False, ()),
                job=self._job(f"q{i}"),
            )
        with CaptureQueriesContext(connection) as ctx:
            questions_panel.build_panel(self.user)
        self.assertLessEqual(len(ctx.captured_queries), 6)

    def test_each_kind_of_change_invalidates_the_cache(self):
        key = panel_cache_key(self.user.pk)
        draft = self.draft(FormField(HEARD, TEXT, False, ()))

        def primed():
            cache.set(key, {"stale": True}, 3600)

        primed()
        with self.captureOnCommitCallbacks(execute=True):
            row = write_answer(self.profile, HEARD, "LinkedIn", "user").row
        self.assertIsNone(cache.get(key), "write_answer")

        primed()
        with self.captureOnCommitCallbacks(execute=True):
            delete_answer(row)
        self.assertIsNone(cache.get(key), "delete_answer")

        primed()
        from apps.auto_apply.services.drafting import _persist_draft

        with self.captureOnCommitCallbacks(execute=True):
            _persist_draft(self.user, self._job("n"), status=AutoApplyDraft.Status.DRAFTED,
                           form_schema_snapshot={"fields": []})
        self.assertIsNone(cache.get(key), "new draft")
        self.assertTrue(draft.pk)

    def test_the_cached_panel_does_not_cross_users(self):
        bob = User.objects.create_user(username="bob", password="pw")
        self.draft(FormField(HEARD, TEXT, False, ()))
        questions_panel.get_panel(self.user)
        self.assertEqual(questions_panel.get_panel(bob)["question_count"], 0)
