import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.accounts.models import Profile

User = get_user_model()


class ProfileSignalTests(TestCase):
    def test_creating_user_creates_exactly_one_profile(self):
        user = User.objects.create_user(username="alice", password="pw")
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)

    def test_new_profile_has_default_field_values(self):
        user = User.objects.create_user(username="bob", password="pw")
        profile = user.profile
        self.assertEqual(profile.target_titles, [])
        self.assertEqual(profile.target_tags, [])
        self.assertEqual(profile.target_locations, [])
        self.assertEqual(profile.excluded_employers, [])
        self.assertIsNone(profile.min_salary)
        self.assertEqual(profile.remote_pref, Profile.RemotePref.ANY)
        self.assertTrue(profile.is_active)

    def test_second_profile_for_same_user_rejected(self):
        user = User.objects.create_user(username="carol", password="pw")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(user=user)

    def test_editing_list_fields_persists_as_json(self):
        user = User.objects.create_user(username="dave", password="pw")
        profile = user.profile
        profile.target_titles = ["Backend Engineer", "Platform Engineer"]
        profile.target_tags = ["python", "kubernetes"]
        profile.save()

        profile.refresh_from_db()
        self.assertEqual(
            profile.target_titles, ["Backend Engineer", "Platform Engineer"]
        )
        self.assertEqual(profile.target_tags, ["python", "kubernetes"])

    def test_updating_user_does_not_create_duplicate_profile(self):
        user = User.objects.create_user(username="erin", password="pw")
        user.email = "erin@example.com"
        user.save()  # not `created` — signal must not add a second profile
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)

    def test_new_profile_has_empty_contact_and_resume_fields(self):
        user = User.objects.create_user(username="frank", password="pw")
        profile = user.profile
        self.assertEqual(profile.phone, "")
        self.assertEqual(profile.linkedin_url, "")
        self.assertFalse(profile.resume)
        self.assertEqual(profile.resume_text, "")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="jobborg-test-media-"))
class ProfileResumeFieldTests(TestCase):
    """U1: Profile.resume/resume_text plus the upload validators and the
    explicit-trigger `set_resume()` helper (docs/plans/
    2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md)."""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings as django_settings

        shutil.rmtree(django_settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(username="grace", password="pw")
        self.profile = self.user.profile

    def test_well_formed_pdf_passes_validation(self):
        self.profile.resume = SimpleUploadedFile(
            "resume.pdf", b"%PDF-1.4 minimal", content_type="application/pdf"
        )
        self.profile.full_clean(validate_unique=False)  # must not raise

    def test_unsupported_extension_rejected_at_model_layer(self):
        self.profile.resume = SimpleUploadedFile(
            "resume.exe", b"MZ\x90\x00", content_type="application/x-msdownload"
        )
        with self.assertRaises(ValidationError):
            self.profile.full_clean(validate_unique=False)

    def test_wrong_content_type_with_allowed_extension_rejected(self):
        # Extension allowlisted, but the browser-reported content_type isn't
        # -- e.g. a renamed file. Both checks must hold, not just the
        # extension.
        self.profile.resume = SimpleUploadedFile(
            "resume.pdf", b"not really a pdf", content_type="application/x-msdownload"
        )
        with self.assertRaises(ValidationError):
            self.profile.full_clean(validate_unique=False)

    def test_oversized_upload_rejected(self):
        with override_settings(RESUME_MAX_UPLOAD_SIZE_BYTES=10):
            self.profile.resume = SimpleUploadedFile(
                "resume.txt", b"this is definitely more than ten bytes", content_type="text/plain"
            )
            with self.assertRaises(ValidationError):
                self.profile.full_clean(validate_unique=False)

    def test_no_resume_uploaded_resume_text_stays_empty(self):
        self.profile.save()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.resume_text, "")
        self.assertFalse(self.profile.resume)

    def test_set_resume_saves_file_and_enqueues_parse_task(self):
        upload = SimpleUploadedFile(
            "resume.txt", b"Experienced engineer", content_type="text/plain"
        )
        with mock.patch("apps.accounts.tasks.parse_resume.delay") as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.profile.set_resume(upload)
                mock_delay.assert_not_called()

        self.profile.refresh_from_db()
        self.assertTrue(self.profile.resume)
        mock_delay.assert_called_once_with(self.profile.pk)

    def test_set_resume_clears_resume_text_pending_reparse(self):
        self.profile.resume_text = "stale text from a previous resume"
        self.profile.save(update_fields=["resume_text"])

        upload = SimpleUploadedFile(
            "resume.txt", b"Fresh resume content", content_type="text/plain"
        )
        with mock.patch("apps.accounts.tasks.parse_resume.delay"):
            self.profile.set_resume(upload)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.resume_text, "")

    def test_storage_name_is_random_and_extension_lowercase(self):
        from apps.accounts.models import resume_upload_path
        first = resume_upload_path(self.profile, "Jane Doe.PDF")
        second = resume_upload_path(self.profile, "Jane Doe.PDF")
        self.assertRegex(first, rf"^resumes/{self.user.pk}/[0-9a-f]{{32}}\.pdf$")
        self.assertNotEqual(first, second)

    def test_committed_resume_validation_does_not_access_storage(self):
        self.profile.resume = "resumes/missing.pdf"
        with mock.patch.object(self.profile.resume.storage, "size", side_effect=AssertionError), \
             mock.patch.object(self.profile.resume.storage, "open", side_effect=AssertionError):
            self.profile.full_clean(validate_unique=False)

    def test_octet_stream_requires_supported_extension(self):
        from apps.accounts.models import validate_resume_file
        validate_resume_file(SimpleUploadedFile("resume.pdf", b"pdf", content_type="application/octet-stream"))
        with self.assertRaises(ValidationError):
            validate_resume_file(SimpleUploadedFile("resume.exe", b"exe", content_type="application/octet-stream"))

    def test_replacing_clearing_and_retaining_resume_storage(self):
        with mock.patch("apps.accounts.tasks.parse_resume.delay"), self.captureOnCommitCallbacks(execute=True):
            self.profile.set_resume(SimpleUploadedFile("one.txt", b"one", content_type="text/plain"))
        old = self.profile.resume.name
        storage = self.profile.resume.storage
        with mock.patch("apps.accounts.tasks.parse_resume.delay"), self.captureOnCommitCallbacks(execute=True):
            self.profile.set_resume(self.profile.resume)
        self.assertTrue(storage.exists(old))
        with mock.patch("apps.accounts.tasks.parse_resume.delay"), self.captureOnCommitCallbacks(execute=True):
            self.profile.set_resume(SimpleUploadedFile("two.txt", b"two", content_type="text/plain"))
            self.assertTrue(storage.exists(old))
        self.assertFalse(storage.exists(old))
        replacement = self.profile.resume.name
        with self.captureOnCommitCallbacks(execute=True):
            self.profile.set_resume(None)
        self.assertFalse(storage.exists(replacement))

    def test_admin_saves_other_fields_before_applying_resume_change(self):
        from types import SimpleNamespace
        from django.contrib.admin import AdminSite
        from apps.accounts.admin import ProfileAdmin
        self.profile.full_name = "Updated name"
        self.profile.resume = SimpleUploadedFile("resume.txt", b"updated", content_type="text/plain")
        with mock.patch("apps.accounts.tasks.parse_resume.delay"), self.captureOnCommitCallbacks(execute=True):
            ProfileAdmin(Profile, AdminSite()).save_model(None, self.profile, SimpleNamespace(changed_data=["resume", "full_name"]), True)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, "Updated name")
        self.assertTrue(self.profile.resume)

    def test_admin_can_create_profile_with_resume(self):
        from types import SimpleNamespace
        from django.contrib.admin import AdminSite
        from apps.accounts.admin import ProfileAdmin
        self.profile.delete()
        profile = Profile(user=self.user, full_name="New profile", resume=SimpleUploadedFile("resume.txt", b"new", content_type="text/plain"))
        with mock.patch("apps.accounts.tasks.parse_resume.delay"), self.captureOnCommitCallbacks(execute=True):
            ProfileAdmin(Profile, AdminSite()).save_model(None, profile, SimpleNamespace(changed_data=["resume"]), False)
        profile.refresh_from_db()
        self.assertTrue(profile.resume)
        self.assertEqual(profile.full_name, "New profile")
