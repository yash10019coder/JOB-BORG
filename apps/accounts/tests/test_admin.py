import tempfile
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from apps.accounts.admin import ProfileAdmin, UnresolvedTargetLocationFilter
from apps.accounts.models import Profile

User = get_user_model()


class UnresolvedTargetLocationFilterTests(TestCase):
    def _profile(self, username, normalized):
        user = User.objects.create_user(username=username, password="pw")
        profile = user.profile
        profile.target_locations_normalized = normalized
        profile.save()
        return profile

    def test_filters_to_profiles_with_an_unresolved_entry(self):
        with_unresolved = self._profile("alice", [
            {"raw": "Xyzzyville", "city": None, "region": None, "country": None, "resolved": False},
        ])
        self._profile("bob", [
            {"raw": "London", "city": "London", "region": None, "country": "UK", "resolved": True},
        ])
        self._profile("carol", [])

        request = RequestFactory().get("/", {"unresolved_target_location": "yes"})
        f = UnresolvedTargetLocationFilter(request, {"unresolved_target_location": ["yes"]},
                                            Profile, None)
        result = f.queryset(request, Profile.objects.all())

        self.assertEqual(list(result), [with_unresolved])

    def test_no_filter_value_returns_everything(self):
        self._profile("dave", [])
        request = RequestFactory().get("/")
        f = UnresolvedTargetLocationFilter(request, {}, Profile, None)
        result = f.queryset(request, Profile.objects.all())
        self.assertEqual(result.count(), Profile.objects.count())


class ProfileAdminSaveModelTests(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.enterContext(self.settings(MEDIA_ROOT=media.name))

    def test_changing_the_resume_does_not_drop_other_edited_fields(self):
        user = User.objects.create_user(username="grace", password="pw")
        profile = user.profile
        upload = SimpleUploadedFile("cv.pdf", b"%PDF-1.4 minimal", content_type="application/pdf")
        profile.full_name = "Grace Hopper"          # edited in the same admin submit
        profile.resume = upload

        form = mock.Mock(changed_data=["resume", "full_name"], cleaned_data={"resume": upload}, initial={"resume": None})
        with mock.patch("apps.accounts.tasks.parse_resume.delay") as parse, self.captureOnCommitCallbacks(execute=True):
            ProfileAdmin(Profile, AdminSite()).save_model(RequestFactory().post("/"), profile, form, True)

        profile.refresh_from_db()
        self.assertEqual(profile.full_name, "Grace Hopper")
        self.assertTrue(profile.resume)
        parse.assert_called_once_with(profile.pk)

    def test_creating_a_profile_with_a_resume_saves_fields_and_queues_parsing(self):
        user = User.objects.create_user(username="new-profile", password="pw")
        user.profile.delete()
        upload = SimpleUploadedFile("resume.txt", b"Grace Hopper")
        profile = Profile(user=user, full_name="Grace Hopper", resume=upload)
        form = mock.Mock(
            changed_data=["resume", "full_name"], cleaned_data={"resume": upload}, initial={}
        )

        with mock.patch("apps.accounts.tasks.parse_resume.delay") as parse, self.captureOnCommitCallbacks(execute=True):
            ProfileAdmin(Profile, AdminSite()).save_model(RequestFactory().post("/"), profile, form, False)

        profile.refresh_from_db()
        self.assertEqual(profile.full_name, "Grace Hopper")
        self.assertTrue(profile.resume)
        self.assertEqual(profile.resume_text, "")
        parse.assert_called_once_with(profile.pk)

    def test_clearing_a_resume_clears_text_without_queuing_parsing(self):
        profile = User.objects.create_user(username="clear-profile", password="pw").profile
        profile.resume = SimpleUploadedFile("resume.txt", b"Old resume")
        profile.resume_text = "Old resume"
        profile.save()
        initial_resume = profile.resume
        profile.resume = None
        profile.full_name = "Updated Name"
        form = mock.Mock(
            changed_data=["resume", "full_name"],
            cleaned_data={"resume": False},
            initial={"resume": initial_resume},
        )

        with mock.patch("apps.accounts.tasks.parse_resume.delay") as parse:
            ProfileAdmin(Profile, AdminSite()).save_model(RequestFactory().post("/"), profile, form, True)

        profile.refresh_from_db()
        self.assertFalse(profile.resume)
        self.assertEqual(profile.resume_text, "")
        self.assertEqual(profile.full_name, "Updated Name")
        parse.assert_not_called()

    def test_replacing_an_existing_resume_swaps_it_and_queues_parsing(self):
        profile = User.objects.create_user(username="replace-profile", password="pw").profile
        profile.resume = SimpleUploadedFile("old.txt", b"Old resume")
        profile.resume_text = "Old resume"
        profile.save()
        original_resume_name = profile.resume.name
        initial_resume = profile.resume
        new_upload = SimpleUploadedFile("new.txt", b"New resume")
        profile.resume = new_upload
        form = mock.Mock(
            changed_data=["resume"],
            cleaned_data={"resume": new_upload},
            initial={"resume": initial_resume},
        )

        with mock.patch("apps.accounts.tasks.parse_resume.delay") as parse, self.captureOnCommitCallbacks(execute=True):
            ProfileAdmin(Profile, AdminSite()).save_model(RequestFactory().post("/"), profile, form, True)

        profile.refresh_from_db()
        self.assertTrue(profile.resume)
        self.assertNotEqual(profile.resume.name, original_resume_name)
        self.assertEqual(profile.resume_text, "")
        parse.assert_called_once_with(profile.pk)
