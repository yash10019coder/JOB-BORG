from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from apps.accounts.admin import UnresolvedTargetLocationFilter
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
    def test_changing_the_resume_does_not_drop_other_edited_fields(self):
        from unittest import mock

        from django.contrib.admin.sites import AdminSite
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.accounts.admin import ProfileAdmin

        user = User.objects.create_user(username="grace", password="pw")
        profile = user.profile
        upload = SimpleUploadedFile("cv.pdf", b"%PDF-1.4 minimal", content_type="application/pdf")
        profile.full_name = "Grace Hopper"          # edited in the same admin submit
        profile.resume = upload

        form = mock.Mock(changed_data=["resume", "full_name"], cleaned_data={"resume": upload}, initial={"resume": None})
        with mock.patch("apps.accounts.tasks.parse_resume.delay"):
            ProfileAdmin(Profile, AdminSite()).save_model(RequestFactory().post("/"), profile, form, True)

        profile.refresh_from_db()
        self.assertEqual(profile.full_name, "Grace Hopper")
        self.assertTrue(profile.resume)
