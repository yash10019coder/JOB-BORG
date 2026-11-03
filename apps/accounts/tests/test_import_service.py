"""Starting, running and sweeping import jobs (rules A1, A2, D7, D8, D10, D11, P1, P3, P4)."""
import json
import os
import tempfile
from datetime import timedelta
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts import tasks
from apps.accounts.importing import service
from apps.accounts.importing.documents import DocumentError
from apps.accounts.models import AnswerBank, AnswerObservation, ImportJob, Profile, ProfileSuggestion, ResumeEntry
from apps.accounts.tests import import_fixtures as fx
from apps.accounts.tests.test_import_documents import build_docx, build_pdf

User = get_user_model()
SENTINEL = "ZXQ-UNRELATED-SENTINEL"


def ascii_lines(text):
    for old, new in (("–", "-"), ("•", ""), ("◦", "-")):
        text = text.replace(old, new)
    return [line for line in text.split("\n")]


def pdf_upload(name="my private resume.pdf", lines=None):
    return SimpleUploadedFile(name, build_pdf(lines or ascii_lines(fx.BULLET_ORG_RESUME)), content_type="application/pdf")


def docx_upload(name="resume.docx", lines=None):
    return SimpleUploadedFile(name, build_docx(lines or fx.BULLET_ORG_RESUME.split("\n")))


class _Base(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile

    def start(self, upload=None, kind=ImportJob.SourceKind.RESUME, profile=None):
        return service.start_document_import(profile or self.profile, kind, upload=upload)

    def run_started(self, upload=None, kind=ImportJob.SourceKind.RESUME, profile=None):
        with self.captureOnCommitCallbacks(execute=True):
            job = self.start(upload if upload is not None else docx_upload(), kind, profile)
        job.refresh_from_db()
        return job

    def stored_files(self):
        return [name for _, _, names in os.walk(self.media.name) for name in names]


class StartTests(_Base):
    def test_A1_a_pending_job_is_created_and_the_task_is_queued_only_after_commit(self):
        with mock.patch("apps.accounts.tasks.run_document_import") as task:
            with self.captureOnCommitCallbacks() as callbacks:
                job = self.start(docx_upload())
            task.delay.assert_not_called()
            for callback in callbacks:
                callback()
        task.delay.assert_called_once_with(str(job.public_id))
        self.assertEqual((job.status, job.kind, job.source_kind), ("pending", "document", "resume"))

    def test_D7_the_upload_is_stored_under_imports_with_a_random_name(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            job = self.start(pdf_upload("my private resume.pdf"))
        self.assertTrue(job.source_file.name.startswith(f"imports/{self.profile.user_id}/"))
        self.assertNotIn("private", job.source_file.name)
        self.assertTrue(job.source_file.name.endswith(".pdf"))

    def test_A1_a_second_import_while_one_is_in_flight_is_refused(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            self.start(docx_upload())
            with self.assertRaises(service.ImportInProgress):
                self.start(docx_upload())
            self.start(docx_upload(), profile=self.other)  # another profile is independent

    def test_A1_a_finished_job_frees_the_slot(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            first = self.start(docx_upload())
            ImportJob.objects.filter(pk=first.pk).update(status=ImportJob.Status.READY)
            self.start(docx_upload())

    def test_A1_the_hourly_rate_limit(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            for _ in range(settings.PROFILE_IMPORT_RATE_PER_HOUR):
                job = self.start(docx_upload())
                ImportJob.objects.filter(pk=job.pk).update(status=ImportJob.Status.APPLIED)
            with self.assertRaises(service.ImportRateLimited):
                self.start(docx_upload())
            self.start(docx_upload(), profile=self.other)  # per user

    def test_A1_the_limit_is_configurable(self):
        with mock.patch("apps.accounts.tasks.run_document_import"), self.settings(PROFILE_IMPORT_RATE_PER_HOUR=1):
            job = self.start(docx_upload())
            ImportJob.objects.filter(pk=job.pk).update(status=ImportJob.Status.APPLIED)
            with self.assertRaises(service.ImportRateLimited):
                self.start(docx_upload())

    def test_a_refused_file_creates_no_job_and_does_not_use_up_the_rate_limit(self):
        bad = {
            "unsupported_type": SimpleUploadedFile("x.exe", b"x" * 500),
            "empty_file": SimpleUploadedFile("x.pdf", b""),
        }
        for code, upload in bad.items():
            with self.subTest(code=code), self.assertRaises(DocumentError) as caught:
                self.start(upload)
            self.assertEqual(caught.exception.code, code)
        with self.settings(PROFILE_IMPORT_MAX_PDF_BYTES=100), self.assertRaises(DocumentError) as caught:
            self.start(docx_upload())
        self.assertEqual(caught.exception.code, "file_too_large")
        self.assertEqual(ImportJob.objects.count(), 0)
        self.assertIsNone(cache.get(service._rate_key(self.profile.user_id, "document")))

    def test_D10_the_saved_resume_needs_no_upload(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            job = self.start(kind=ImportJob.SourceKind.CURRENT_RESUME)
        self.assertEqual((job.source_kind, bool(job.source_file)), ("current_resume", False))

    def test_an_upload_is_required_for_a_file_source(self):
        with self.assertRaises(DocumentError):
            self.start(None)


class RunTests(_Base):
    def test_a_resume_becomes_a_ready_job_with_proposals(self):
        job = self.run_started(docx_upload())
        self.assertEqual((job.status, job.extractor, job.error_code), ("ready", "rule", ""))
        payload = job.payload
        self.assertEqual(payload["v"], service.PAYLOAD_VERSION)
        self.assertEqual(payload["fields"]["full_name"]["value"], "Jane Doe")
        self.assertEqual(payload["fields"]["current_employer"]["value"], "Acme Payments")
        self.assertEqual(payload["fields"]["phone"]["value"], "+91 9876543210")
        self.assertEqual([e["organization"] for e in payload["entries"]], ["Acme Payments", "Globex Data"])
        self.assertEqual(payload["entries"][0]["start"], "2021-01")
        self.assertTrue(payload["entries"][0]["is_current"])
        self.assertIn("python", payload["fields"]["target_tags"]["value"])
        self.assertEqual(set(payload["meta"]), {"truncated", "linkedin_layout", "skills_lexicon", "title_lexicon"})
        self.assertGreater(job.expires_at, timezone.now() + timedelta(hours=23))

    def test_a_pdf_upload_works_too(self):
        job = self.run_started(pdf_upload())
        self.assertEqual(job.status, "ready")
        self.assertEqual(job.payload["fields"]["full_name"]["value"], "Jane Doe")

    def test_a_linkedin_pdf_goes_through_the_same_pipeline(self):
        job = self.run_started(pdf_upload(), kind=ImportJob.SourceKind.LINKEDIN_PDF)
        self.assertEqual((job.source_kind, job.status), ("linkedin_pdf", "ready"))

    def test_D8_the_uploaded_file_is_deleted_after_it_is_read(self):
        job = self.run_started(docx_upload())
        self.assertFalse(job.source_file)
        self.assertEqual(self.stored_files(), [])

    def test_a_job_never_writes_to_the_profile_or_to_the_learning_tables(self):
        before = Profile.objects.values().get(pk=self.profile.pk)
        self.run_started(docx_upload())
        self.assertEqual(Profile.objects.values().get(pk=self.profile.pk), before)
        self.assertEqual(ResumeEntry.objects.count(), 0)
        for model in (AnswerObservation, AnswerBank, ProfileSuggestion):  # P4 / FR9.11
            self.assertEqual(model.objects.count(), 0)

    def test_P1_the_payload_holds_proposals_and_snippets_not_the_resume_text(self):
        lines = fx.BULLET_ORG_RESUME.split("\n") + [f"A distinctive unrelated sentence {SENTINEL} about hobbies."]
        job = self.run_started(docx_upload(lines=lines))
        dumped = json.dumps(job.payload)
        self.assertNotIn(SENTINEL, dumped)
        self.assertLess(len(dumped), 12_000)
        for entry in list(job.payload["fields"].values()) + job.payload["entries"]:
            self.assertLessEqual(len(entry["snippet"]), 80)

    def test_D10_the_saved_resume_text_is_used_without_an_upload(self):
        Profile.objects.filter(pk=self.profile.pk).update(resume_text=fx.BULLET_ORG_RESUME)
        self.profile.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            job = service.start_document_import(self.profile, ImportJob.SourceKind.CURRENT_RESUME)
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        self.assertEqual(job.payload["fields"]["full_name"]["value"], "Jane Doe")

    def test_D10_an_empty_saved_resume_fails_with_a_code(self):
        with self.captureOnCommitCallbacks(execute=True):
            job = service.start_document_import(self.profile, ImportJob.SourceKind.CURRENT_RESUME)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), ("failed", "no_resume_text"))

    def test_D11_unsafe_documents_fail_with_a_short_code_and_leave_no_file(self):
        cases = {
            "pdf_encrypted": None,
            "pdf_active_content": SimpleUploadedFile("a.pdf", build_pdf(catalog_extra=b"/OpenAction << /S /JavaScript /JS (x) >>")),
            "no_text_found": SimpleUploadedFile("a.pdf", build_pdf(["tiny"])),
            "docx_invalid": SimpleUploadedFile("a.docx", b"not a zip" * 100),
        }
        from pypdf import PdfReader, PdfWriter
        import io

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf())))
        writer.encrypt("secret")
        out = io.BytesIO()
        writer.write(out)
        cases["pdf_encrypted"] = SimpleUploadedFile("a.pdf", out.getvalue())
        for code, upload in cases.items():
            with self.subTest(code=code):
                job = self.run_started(upload)
                self.assertEqual((job.status, job.error_code, job.payload), ("failed", code, {}))
                self.assertEqual(self.stored_files(), [])
                self.assertTrue(service.error_message(code))

    def test_every_error_code_has_a_friendly_message(self):
        for code in ("unsupported_type", "empty_file", "file_too_large", "not_a_pdf", "pdf_encrypted", "pdf_too_long",
                     "pdf_active_content", "pdf_unreadable", "docx_invalid", "docx_unreadable", "docx_macros",
                     "docx_unsafe", "docx_too_complex", "docx_too_large", "txt_unreadable", "no_text_found",
                     "no_resume_text", "nothing_found", "timeout", "unexpected"):
            self.assertIn(code, service.ERROR_MESSAGES)
        self.assertEqual(service.error_message("something_new"), service.ERROR_MESSAGES["unexpected"])

    def test_a_document_with_nothing_recognisable_fails_as_nothing_found(self):
        job = self.run_started(docx_upload(lines=["lorem ipsum dolor sit amet " * 20]))
        self.assertEqual((job.status, job.error_code), ("failed", "nothing_found"))

    def test_D11_an_unexpected_error_is_coded_and_its_text_is_never_logged(self):
        with mock.patch.object(service, "run_rules", side_effect=ValueError(SENTINEL)):
            with self.assertLogs("apps.accounts.importing.service", level="INFO") as logs:
                job = self.run_started(docx_upload())
        self.assertEqual((job.status, job.error_code), ("failed", "unexpected"))
        output = "\n".join(logs.output)
        self.assertNotIn(SENTINEL, output)
        self.assertIn("ValueError", output)
        self.assertEqual(self.stored_files(), [])

    def test_P3_resume_text_never_reaches_the_logs(self):
        lines = fx.BULLET_ORG_RESUME.split("\n") + [f"Hobbies: {SENTINEL} and gardening."]
        with self.assertLogs("apps.accounts.importing.service", level="INFO") as logs:
            self.run_started(docx_upload(lines=lines))
        self.assertNotIn(SENTINEL, "\n".join(logs.output))

    def test_a_soft_time_limit_is_a_timeout_not_a_crash(self):
        with mock.patch.object(service, "run_rules", side_effect=SoftTimeLimitExceeded()):
            job = self.run_started(docx_upload())
        self.assertEqual((job.status, job.error_code), ("failed", "timeout"))

    def test_the_task_is_idempotent_and_ignores_jobs_that_are_not_pending(self):
        job = self.run_started(docx_upload())
        payload = job.payload
        self.assertIsNone(tasks.run_document_import(str(job.public_id)))
        job.refresh_from_db()
        self.assertEqual(job.payload, payload)
        running = ImportJob.objects.create(profile=self.other, kind="document", source_kind="resume", status="running")
        self.assertIsNone(tasks.run_document_import(str(running.public_id)))
        running.refresh_from_db()
        self.assertEqual(running.status, "running")

    def test_an_unknown_public_id_does_nothing(self):
        import uuid

        self.assertIsNone(tasks.run_document_import(str(uuid.uuid4())))

    def test_the_task_reports_its_final_status(self):
        with mock.patch("apps.accounts.tasks.run_document_import"):
            job = self.start(docx_upload())
        self.assertEqual(tasks.run_document_import(str(job.public_id)), "ready")

    def test_task_limits_come_from_settings(self):
        self.assertEqual(tasks.run_document_import.time_limit, settings.PROFILE_IMPORT_TASK_TIME_LIMIT_SECONDS)
        self.assertEqual(tasks.run_document_import.soft_time_limit, settings.PROFILE_IMPORT_TASK_SOFT_TIME_LIMIT_SECONDS)


class SweepTests(_Base):
    def job(self, status, profile=None, **extra):
        return ImportJob.objects.create(
            profile=profile or self.profile, kind="document", source_kind="resume", status=status, **extra
        )

    def age(self, job, **delta):
        ImportJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(**delta))

    def test_P1_ready_jobs_past_their_expiry_lose_their_payload(self):
        old = self.job("ready", payload={"fields": {"phone": {"value": "x"}}}, expires_at=timezone.now() - timedelta(minutes=1))
        fresh = self.job("ready", profile=self.other, payload={"fields": {}}, expires_at=timezone.now() + timedelta(hours=5))
        counts = tasks.sweep_import_jobs()
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual((old.status, old.payload), ("expired", {}))
        self.assertEqual(fresh.status, "ready")
        self.assertEqual(counts["expired"], 1)

    def test_A2_jobs_stuck_in_flight_are_failed_as_timeouts(self):
        for status, profile in (("running", self.profile), ("pending", self.other)):
            job = self.job(status, profile=profile)
            self.age(job, minutes=settings.PROFILE_IMPORT_STUCK_MINUTES + 1)
            tasks.sweep_import_jobs()
            job.refresh_from_db()
            self.assertEqual((job.status, job.error_code), ("failed", "timeout"), status)

    def test_A2_recent_in_flight_jobs_are_left_alone(self):
        job = self.job("running")
        self.age(job, minutes=5)
        tasks.sweep_import_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "running")

    def test_D8_a_file_left_behind_by_a_finished_job_is_removed(self):
        from django.core.files.base import ContentFile

        job = self.job("failed")
        job.source_file.save("x.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.assertEqual(len(self.stored_files()), 1)
        tasks.sweep_import_jobs()
        job.refresh_from_db()
        self.assertFalse(job.source_file)
        self.assertEqual(self.stored_files(), [])

    def test_P1_old_rows_are_deleted_but_in_flight_and_recent_rows_are_kept(self):
        old_done = self.job("applied")
        old_running = self.job("running", profile=self.other)
        recent = self.job("failed")
        cutoff = timezone.now() - timedelta(days=settings.PROFILE_IMPORT_ROW_RETENTION_DAYS + 1)
        ImportJob.objects.filter(pk__in=[old_done.pk, old_running.pk]).update(created_at=cutoff)
        # keep the running job from being failed first, so it is the in-flight rule under test
        ImportJob.objects.filter(pk=old_running.pk).update(updated_at=timezone.now())
        tasks.sweep_import_jobs()
        self.assertFalse(ImportJob.objects.filter(pk=old_done.pk).exists())
        self.assertTrue(ImportJob.objects.filter(pk=old_running.pk).exists())
        self.assertTrue(ImportJob.objects.filter(pk=recent.pk).exists())

    def test_the_sweep_is_idempotent(self):
        self.job("ready", expires_at=timezone.now() - timedelta(minutes=1), payload={"a": 1})
        tasks.sweep_import_jobs()
        self.assertEqual(tasks.sweep_import_jobs(), {"expired": 0, "failed": 0, "deleted": 0})

    def test_the_sweep_is_scheduled_every_fifteen_minutes(self):
        entry = settings.CELERY_BEAT_SCHEDULE["import-job-sweep"]
        self.assertEqual(entry["task"], "apps.accounts.sweep_import_jobs")
