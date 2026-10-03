from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import AnswerBank
from apps.accounts.services import profile_fields

User = get_user_model()


class _ProfileTestCase(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.mock_schedule = patcher.start()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = self.user.profile
        # Creating the user already fires the post_save rematch hook once.
        self.mock_schedule.reset_mock()


class RecordUserEditsTests(_ProfileTestCase):
    def test_stamps_only_changed_fields(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.phone = "555-0100"
        profile_fields.record_user_edits(self.profile, before)
        self.assertEqual(list(self.profile.field_provenance), ["phone"])
        self.assertEqual(self.profile.field_provenance["phone"]["source"], "user")
        self.assertFalse(self.profile.field_provenance["phone"]["locked"])

    def test_unchanged_fields_are_not_stamped(self):
        self.profile.phone = "555-0100"
        before = profile_fields.snapshot(self.profile)
        profile_fields.record_user_edits(self.profile, before)
        self.assertEqual(self.profile.field_provenance, {})

    def test_dict_fields_are_stamped_per_key(self):
        self.profile.visa_status_by_country = {"USA": "h1b", "IND": "citizen"}
        before = profile_fields.snapshot(self.profile)
        self.profile.visa_status_by_country = {"USA": "opt", "IND": "citizen"}
        profile_fields.record_user_edits(self.profile, before)
        self.assertEqual(list(self.profile.field_provenance), ["visa_status_by_country.USA"])

    def test_removed_dict_key_drops_its_entry(self):
        self.profile.salary_by_region = {"US": "150-200k"}
        self.profile.field_provenance = {
            "salary_by_region.US": {"source": "user", "locked": False}
        }
        before = profile_fields.snapshot(self.profile)
        self.profile.salary_by_region = {}
        profile_fields.record_user_edits(self.profile, before)
        self.assertEqual(self.profile.field_provenance, {})

    def test_editing_preserves_an_existing_lock(self):
        self.profile.github_url = "https://github.com/a"
        self.profile.field_provenance = {
            "github_url": {"source": "user", "locked": True}
        }
        before = profile_fields.snapshot(self.profile)
        self.profile.github_url = "https://github.com/b"
        profile_fields.record_user_edits(self.profile, before)
        self.assertTrue(self.profile.field_provenance["github_url"]["locked"])


class ApplyProfileFieldTests(_ProfileTestCase):
    def test_writes_into_an_empty_field_and_records_the_source(self):
        applied = profile_fields.apply_profile_field(
            self.profile, "portfolio_url", "https://x.dev", "imported", detail="resume_llm"
        )
        self.assertTrue(applied)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.portfolio_url, "https://x.dev")
        entry = self.profile.field_provenance["portfolio_url"]
        self.assertEqual((entry["source"], entry["detail"]), ("imported", "resume_llm"))

    def test_legacy_non_empty_value_without_an_entry_counts_as_user(self):
        self.profile.phone = "555-0100"
        self.profile.save()
        applied = profile_fields.apply_profile_field(
            self.profile, "phone", "999", "imported"
        )
        self.assertFalse(applied)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone, "555-0100")

    def test_user_set_field_is_kept_against_learned_and_imported(self):
        self.profile.phone = "555-0100"
        self.profile.field_provenance = {"phone": {"source": "user", "locked": False}}
        self.profile.save()
        for source in ("learned", "imported"):
            with self.subTest(source=source):
                self.assertFalse(
                    profile_fields.apply_profile_field(self.profile, "phone", "999", source)
                )

    def test_learned_overwrites_imported_but_not_the_reverse(self):
        profile_fields.apply_profile_field(self.profile, "full_name", "A", "imported")
        self.assertFalse(
            profile_fields.apply_profile_field(self.profile, "full_name", "B", "imported")
        )
        self.assertTrue(
            profile_fields.apply_profile_field(self.profile, "full_name", "C", "learned")
        )
        self.assertFalse(
            profile_fields.apply_profile_field(self.profile, "full_name", "D", "imported")
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, "C")

    def test_keyed_field_writes_one_key_and_keeps_the_rest(self):
        self.profile.salary_by_region = {"US": "150-200k"}
        self.profile.field_provenance = {
            "salary_by_region.US": {"source": "user", "locked": False}
        }
        self.profile.save()
        self.assertFalse(
            profile_fields.apply_profile_field(
                self.profile, "salary_by_region.US", "200-250k", "learned"
            )
        )
        self.assertTrue(
            profile_fields.apply_profile_field(
                self.profile, "salary_by_region.EU", "100-150k", "learned"
            )
        )
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.salary_by_region, {"US": "150-200k", "EU": "100-150k"}
        )

    def test_locked_field_is_kept(self):
        self.profile.phone = "555"
        self.profile.field_provenance = {"phone": {"source": "user", "locked": True}}
        self.profile.save()
        self.assertFalse(profile_fields.apply_profile_field(self.profile, "phone", "9", "learned"))

    def test_rejects_user_source_and_uncovered_fields(self):
        with self.assertRaises(ValueError):
            profile_fields.apply_profile_field(self.profile, "phone", "1", "user")
        with self.assertRaises(ValueError):
            profile_fields.apply_profile_field(self.profile, "headline", "x", "imported")
        with self.assertRaises(ValueError):
            profile_fields.apply_profile_field(self.profile, "salary_by_region", {}, "imported")

    def test_one_save_means_one_rematch_trigger(self):
        profile_fields.apply_profile_field(self.profile, "phone", "1", "imported")
        self.assertEqual(self.mock_schedule.call_count, 1)

    def test_default_source_enum_values_are_the_answerbank_sources(self):
        self.assertEqual(
            {s.value for s in AnswerBank.Source}, {"user", "learned", "imported"}
        )


class ProfileFormProvenanceTests(_ProfileTestCase):
    def _post(self, **overrides):
        data = {
            "full_name": "", "headline": "", "phone": "", "linkedin_url": "",
            "github_url": "", "portfolio_url": "", "current_employer": "",
            "target_titles": "", "target_tags": "", "target_locations": "",
            "excluded_employers": "", "remote_pref": "any", "is_active": "on",
            "preferred_currency": "USD",
        }
        data.update(overrides)
        self.client.force_login(self.user)
        return self.client.post(reverse("profile"), data)

    def test_form_save_stamps_changed_fields_as_user(self):
        self._post(phone="555-0100", target_tags="python, django")
        self.profile.refresh_from_db()
        self.assertEqual(
            set(self.profile.field_provenance), {"phone", "target_tags"}
        )

    def test_untouched_resave_adds_no_new_entries_and_keeps_old_ones(self):
        self._post(phone="555-0100")
        first = dict(self.profile.__class__.objects.get(pk=self.profile.pk).field_provenance)
        self._post(phone="555-0100")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.field_provenance, first)

    def test_visa_repeater_row_is_stamped_per_country(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("profile"),
            {
                "remote_pref": "any", "is_active": "on", "preferred_currency": "USD",
                "visa_country[]": ["USA"], "visa_status[]": ["h1b"],
            },
        )
        self.profile.refresh_from_db()
        self.assertIn("visa_status_by_country.USA", self.profile.field_provenance)

    def test_form_save_triggers_exactly_one_rematch(self):
        self._post(phone="555-0100")
        self.assertEqual(self.mock_schedule.call_count, 1)

    def test_user_edit_then_import_is_kept(self):
        self._post(portfolio_url="https://me.dev")
        self.profile.refresh_from_db()
        self.assertFalse(
            profile_fields.apply_profile_field(
                self.profile, "portfolio_url", "https://other.dev", "imported"
            )
        )
