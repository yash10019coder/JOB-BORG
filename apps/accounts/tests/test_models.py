from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

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
