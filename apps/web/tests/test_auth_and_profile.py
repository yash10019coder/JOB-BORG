from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Profile

User = get_user_model()


class AuthProfileTests(TestCase):
    def setUp(self):
        # Don't fire real rematch enqueues when profiles are saved in tests.
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.mock_schedule = patcher.start()

    def test_anonymous_profile_access_redirects_to_login(self):
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_anonymous_recommendations_access_redirects_to_login(self):
        resp = self.client.get(reverse("recommendations"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_signup_creates_user_and_profile(self):
        resp = self.client.post(
            reverse("signup"),
            {"username": "newbie", "password1": "s3cretpass123", "password2": "s3cretpass123"},
        )
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="newbie")
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)

    def test_valid_profile_submission_persists_parsed_lists(self):
        user = User.objects.create_user(username="alice", password="pw")
        self.client.force_login(user)
        resp = self.client.post(
            reverse("profile"),
            {
                "full_name": "Alice A",
                "headline": "",
                "target_titles": "Backend Engineer, Platform Engineer",
                "target_tags": "python, kubernetes",
                "target_locations": "",
                "excluded_employers": "",
                "min_salary": "120000",
                "remote_pref": Profile.RemotePref.REMOTE_ONLY,
                "is_active": "on",
            },
        )
        self.assertRedirects(resp, reverse("recommendations"), fetch_redirect_response=False)
        profile = User.objects.get(username="alice").profile
        self.assertEqual(profile.target_titles, ["Backend Engineer", "Platform Engineer"])
        self.assertEqual(profile.target_tags, ["python", "kubernetes"])
        self.assertEqual(profile.min_salary, 120000)
        self.assertEqual(profile.remote_pref, Profile.RemotePref.REMOTE_ONLY)

    def test_target_locations_normalized_on_save(self):
        user = User.objects.create_user(username="carol", password="pw")
        self.client.force_login(user)
        self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "",
                "target_locations": "New York, London",
                "excluded_employers": "",
                "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        profile = User.objects.get(username="carol").profile
        self.assertEqual(len(profile.target_locations_normalized), 2)
        resolved_countries = {e["country"] for e in profile.target_locations_normalized}
        self.assertEqual(resolved_countries, {"US", "UK"})
        self.assertTrue(all(e["resolved"] for e in profile.target_locations_normalized))
        self.assertEqual(profile.target_locations_alias_version, "v1")

    def test_target_locations_normalized_empty_when_no_locations(self):
        user = User.objects.create_user(username="dave", password="pw")
        self.client.force_login(user)
        self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "",
                "target_locations": "",
                "excluded_employers": "",
                "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        profile = User.objects.get(username="dave").profile
        self.assertEqual(profile.target_locations_normalized, [])

    def test_target_locations_normalized_keeps_unresolved_entries(self):
        user = User.objects.create_user(username="erin", password="pw")
        self.client.force_login(user)
        self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "",
                "target_locations": "New York, Xyzzyville",
                "excluded_employers": "",
                "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        profile = User.objects.get(username="erin").profile
        self.assertEqual(len(profile.target_locations_normalized), 2)
        resolved_flags = sorted(e["resolved"] for e in profile.target_locations_normalized)
        self.assertEqual(resolved_flags, [False, True])

    def test_target_locations_normalized_dedupes_on_structured_tuple(self):
        user = User.objects.create_user(username="frank", password="pw")
        self.client.force_login(user)
        self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "",
                "target_locations": "NYC, New York",
                "excluded_employers": "",
                "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        profile = User.objects.get(username="frank").profile
        # Raw list preserves both user-typed entries (CSV round-trip)...
        self.assertEqual(profile.target_locations, ["NYC", "New York"])
        # ...but the structured mirror collapses them to one location.
        self.assertEqual(len(profile.target_locations_normalized), 1)

    def test_saving_profile_enqueues_rematch(self):
        user = User.objects.create_user(username="bob", password="pw")
        self.client.force_login(user)
        self.mock_schedule.reset_mock()
        self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "python",
                "target_locations": "", "excluded_employers": "",
                "min_salary": "", "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        self.mock_schedule.assert_called_with(user.profile.pk)

    def test_invalid_min_salary_reders_errors_and_does_not_save(self):
        user = User.objects.create_user(username="carol", password="pw")
        self.client.force_login(user)
        resp = self.client.post(
            reverse("profile"),
            {
                "full_name": "", "headline": "",
                "target_titles": "", "target_tags": "python",
                "target_locations": "", "excluded_employers": "",
                "min_salary": "not-a-number", "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
        self.assertContains(resp, "error", status_code=200)
        profile = User.objects.get(username="carol").profile
        self.assertEqual(profile.target_tags, [])  # nothing saved

    def test_user_cannot_edit_another_users_profile(self):
        alice = User.objects.create_user(username="alice", password="pw")
        bob = User.objects.create_user(username="bob", password="pw")
        self.client.force_login(alice)
        self.client.post(
            reverse("profile"),
            {
                "full_name": "Alice", "headline": "",
                "target_titles": "", "target_tags": "python",
                "target_locations": "", "excluded_employers": "",
                "min_salary": "", "remote_pref": Profile.RemotePref.ANY,
                "is_active": "on",
            },
        )
        # Bob's profile is untouched — the view only ever edits request.user's.
        self.assertEqual(Profile.objects.get(user=bob).target_tags, [])
        self.assertEqual(Profile.objects.get(user=alice).target_tags, ["python"])
