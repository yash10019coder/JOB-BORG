"""Schema-level rules for the profile importer (docs/plans/2026-10-05-002-profile-import-rules.md)."""
import uuid
from datetime import date, timedelta
from unittest import mock

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ImportJob, Profile, ResumeEntry
from apps.accounts.services import profile_fields

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile

    def job(self, profile=None, status=ImportJob.Status.PENDING, **extra):
        return ImportJob.objects.create(
            profile=profile or self.profile,
            kind=ImportJob.Kind.DOCUMENT,
            source_kind=ImportJob.SourceKind.RESUME,
            status=status,
            **extra,
        )


class ImportJobTests(_Base):
    def test_A1_a_second_in_flight_job_for_the_same_profile_is_rejected(self):
        for first in (ImportJob.Status.PENDING, ImportJob.Status.RUNNING):
            for second in (ImportJob.Status.PENDING, ImportJob.Status.RUNNING):
                with self.subTest(first=first, second=second):
                    ImportJob.objects.all().delete()
                    self.job(status=first)
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        self.job(status=second)

    def test_A1_finished_jobs_do_not_block_a_new_one_and_other_profiles_are_independent(self):
        for done in (
            ImportJob.Status.READY, ImportJob.Status.APPLIED, ImportJob.Status.DISCARDED,
            ImportJob.Status.FAILED, ImportJob.Status.EXPIRED,
        ):
            self.job(status=done)
        self.job(status=ImportJob.Status.PENDING)
        self.job(profile=self.other, status=ImportJob.Status.PENDING)
        self.assertEqual(ImportJob.objects.count(), 7)

    def test_A1_a_running_job_that_finishes_frees_the_slot(self):
        running = self.job(status=ImportJob.Status.RUNNING)
        running.status = ImportJob.Status.READY
        running.save(update_fields=["status"])
        self.job(status=ImportJob.Status.PENDING)

    def test_defaults(self):
        job = self.job()
        self.assertEqual(job.extractor, ImportJob.Extractor.RULE)
        self.assertEqual(job.payload, {})
        self.assertEqual(job.error_code, "")
        self.assertIsNone(job.applied_at)
        self.assertIsInstance(job.public_id, uuid.UUID)

    def test_P1_expiry_defaults_to_the_configured_ttl(self):
        before = timezone.now()
        job = self.job()
        self.assertAlmostEqual(
            (job.expires_at - before).total_seconds(), 24 * 3600, delta=60
        )
        with self.settings(PROFILE_IMPORT_TTL_HOURS=2):
            short = self.job(profile=self.other)
        self.assertAlmostEqual((short.expires_at - before).total_seconds(), 2 * 3600, delta=60)

    def test_public_ids_are_unique_per_job(self):
        a, b = self.job(), self.job(profile=self.other)
        self.assertNotEqual(a.public_id, b.public_id)

    def test_D7_uploads_use_a_random_name_outside_the_resumes_folder(self):
        from django.core.files.base import ContentFile

        job = self.job()
        job.source_file.save("My Private Resume.PDF", ContentFile(b"%PDF-1.4"), save=True)
        name = job.source_file.name
        self.assertTrue(name.startswith(f"imports/{self.profile.user_id}/"), name)
        self.assertNotIn("Private", name)
        self.assertTrue(name.endswith(".pdf"))
        job.source_file.delete(save=False)


class ProfileConsentFieldTests(_Base):
    def test_L2_consent_is_off_by_default(self):
        self.assertIsNone(self.profile.llm_import_consent_at)
        self.assertEqual(self.profile.llm_import_consent_version, "")

    def test_consent_can_be_saved_without_triggering_a_rematch(self):
        with mock.patch("apps.matching.signals.schedule_rematch") as schedule:
            self.profile.llm_import_consent_at = timezone.now()
            self.profile.llm_import_consent_version = "2026-10"
            self.profile.save(update_fields=["llm_import_consent_at", "llm_import_consent_version"])
        schedule.assert_not_called()


class ResumeEntryTests(_Base):
    def entry(self, **extra):
        values = dict(
            profile=self.profile, kind=ResumeEntry.Kind.EXPERIENCE, title="Backend Engineer",
            organization="Acme", start_date=date(2022, 1, 1), natural_key="acme|backend engineer|2022-01",
        )
        values.update(extra)
        return ResumeEntry.objects.create(**values)

    def test_R7_the_same_natural_key_cannot_be_stored_twice_for_a_kind(self):
        self.entry()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.entry()

    def test_R7_the_same_key_is_fine_for_another_kind_or_another_profile(self):
        self.entry()
        self.entry(kind=ResumeEntry.Kind.PROJECT)
        self.entry(profile=self.other)
        self.assertEqual(ResumeEntry.objects.count(), 3)

    def test_defaults(self):
        entry = self.entry()
        self.assertEqual(entry.source, ResumeEntry.Source.IMPORTED)
        self.assertEqual(entry.precision, ResumeEntry.Precision.MONTH)
        self.assertEqual(entry.skills, [])
        self.assertFalse(entry.is_current)

    def test_P2_entries_are_deleted_with_the_account(self):
        self.entry()
        self.profile.user.delete()
        self.assertEqual(ResumeEntry.objects.count(), 0)

    def test_ordering_puts_current_then_most_recent_first(self):
        old = self.entry(title="Old", natural_key="a|old|2018-01", start_date=date(2018, 1, 1))
        new = self.entry(title="New", natural_key="a|new|2022-01", start_date=date(2022, 1, 1))
        cur = self.entry(title="Cur", natural_key="a|cur|2020-01", start_date=date(2020, 1, 1), is_current=True)
        self.assertEqual(list(ResumeEntry.objects.filter(profile=self.profile)), [cur, new, old])


class ProvenanceCoverageTests(_Base):
    def test_headline_and_target_titles_are_provenance_covered(self):
        self.assertIn("headline", profile_fields.COVERED_FIELDS)
        self.assertIn("target_titles", profile_fields.COVERED_FIELDS)

    def test_a_user_edit_of_headline_and_titles_is_stamped_as_user(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.headline = "Backend engineer"
        self.profile.target_titles = ["Backend Engineer"]
        profile_fields.record_user_edits(self.profile, before)
        for key in ("headline", "target_titles"):
            entry = self.profile.field_provenance[key]
            self.assertEqual(entry["source"], "user", key)

    def test_a_legacy_value_with_no_entry_counts_as_the_users(self):
        self.profile.headline = "Old headline"
        self.assertEqual(profile_fields.provenance_for(self.profile, "headline")["source"], "user")

    def test_an_importer_cannot_overwrite_a_user_set_headline(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.headline = "Mine"
        profile_fields.record_user_edits(self.profile, before)
        self.profile.save()
        self.assertFalse(
            profile_fields.apply_profile_field(self.profile, "headline", "Theirs", "imported")
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.headline, "Mine")


class AdminTests(_Base):
    def test_P1_the_import_job_admin_hides_the_payload_and_file(self):
        admin = site._registry[ImportJob]
        self.assertIn("payload", admin.exclude)
        self.assertIn("source_file", admin.exclude)
        self.assertFalse(admin.has_add_permission(mock.Mock()))

    def test_resume_entries_cannot_be_added_in_the_admin(self):
        self.assertFalse(site._registry[ResumeEntry].has_add_permission(mock.Mock()))
