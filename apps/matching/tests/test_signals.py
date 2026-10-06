from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.matching.services import MATCHING_PROFILE_FIELDS, profile_snapshot

User = get_user_model()


class RematchGuardTests(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()

    def test_creating_a_profile_rematches(self):
        User.objects.create_user(username="alice", password="pw")
        self.assertEqual(self.schedule.call_count, 1)

    def test_a_full_save_rematches(self):
        profile = User.objects.create_user(username="alice", password="pw").profile
        self.schedule.reset_mock()
        profile.phone = "555-0100"
        profile.save()
        self.assertEqual(self.schedule.call_count, 1)

    def test_a_save_touching_only_non_matching_fields_does_not_rematch(self):
        profile = User.objects.create_user(username="alice", password="pw").profile
        self.schedule.reset_mock()
        profile.phone = "555-0100"
        profile.visa_status_by_country = {"USA": "citizen"}
        profile.save(update_fields=["phone", "visa_status_by_country", "updated_at"])
        profile.save(update_fields=["field_provenance"])
        self.assertEqual(self.schedule.call_count, 0)

    def test_a_save_touching_any_matching_field_rematches_once(self):
        profile = User.objects.create_user(username="alice", password="pw").profile
        for field in ("target_tags", "min_salary", "remote_pref", "is_active"):
            self.schedule.reset_mock()
            profile.save(update_fields=[field, "updated_at"])
            self.assertEqual(self.schedule.call_count, 1, field)

    def test_a_mixed_update_fields_list_rematches(self):
        profile = User.objects.create_user(username="alice", password="pw").profile
        self.schedule.reset_mock()
        profile.save(update_fields=["phone", "target_titles"])
        self.assertEqual(self.schedule.call_count, 1)


class MatchingFieldSetTests(SimpleTestCase):
    def test_every_snapshot_input_is_a_guarded_field(self):
        """If matching starts reading a new Profile column, the guard set must
        grow with it or edits to that column would stop triggering rematches."""
        class Stub:
            def __getattr__(self, name):
                return None

        self.assertLessEqual(set(profile_snapshot(Stub())), MATCHING_PROFILE_FIELDS)
        self.assertIn("is_active", MATCHING_PROFILE_FIELDS)

    def test_contact_and_answer_fields_are_not_guarded(self):
        for field in ("phone", "visa_status_by_country", "citizenship_countries",
                      "salary_by_region", "field_provenance", "resume_text"):
            self.assertNotIn(field, MATCHING_PROFILE_FIELDS)
