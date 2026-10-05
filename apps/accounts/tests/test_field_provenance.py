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
            profile_fields.apply_profile_field(self.profile, "remote_pref", "onsite_only", "imported")
        with self.assertRaises(ValueError):
            profile_fields.apply_profile_field(self.profile, "salary_by_region", {}, "imported")

    def test_a_matching_field_write_means_one_rematch_trigger(self):
        profile_fields.apply_profile_field(self.profile, "target_tags", ["go"], "imported")
        self.assertEqual(self.mock_schedule.call_count, 1)

    def test_a_non_matching_field_write_does_not_rematch(self):
        profile_fields.apply_profile_field(self.profile, "phone", "1", "imported")
        self.assertEqual(self.mock_schedule.call_count, 0)

    def test_default_source_enum_values_are_the_answerbank_sources(self):
        self.assertEqual(
            {s.value for s in AnswerBank.Source}, {"user", "learned", "imported"}
        )


class LocationFieldProvenanceTests(_ProfileTestCase):
    def test_new_contact_fields_are_covered(self):
        for field in ("location_city", "location_country", "mailing_address", "working_timezone"):
            self.assertIn(field, profile_fields.COVERED_FIELDS)

    def test_user_edit_stamps_the_new_fields(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.location_country = "IND"
        self.profile.location_city = "Pune"
        profile_fields.record_user_edits(self.profile, before)
        self.assertEqual(
            sorted(self.profile.field_provenance), ["location_city", "location_country"]
        )

    def test_importer_cannot_overwrite_a_user_set_country(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.location_country = "IND"
        profile_fields.record_user_edits(self.profile, before)
        self.profile.save()
        self.assertFalse(
            profile_fields.apply_profile_field(self.profile, "location_country", "USA", "imported")
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.location_country, "IND")

    def test_importer_may_fill_an_empty_country_and_it_is_recorded_as_imported(self):
        self.assertTrue(
            profile_fields.apply_profile_field(self.profile, "location_country", "USA", "imported")
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.location_country, "USA")
        self.assertEqual(self.profile.field_provenance["location_country"]["source"], "imported")

    def test_writing_a_location_field_does_not_rematch(self):
        profile_fields.apply_profile_field(self.profile, "location_city", "Pune", "imported")
        self.assertEqual(self.mock_schedule.call_count, 0)


class ProfileFormProvenanceTests(_ProfileTestCase):
    """The two profile pages stamp provenance for the fields each one owns."""

    def _post_search(self, **overrides):
        data = {
            "headline": "", "target_titles": "", "target_tags": "", "target_locations": "",
            "excluded_employers": "", "remote_pref": "any", "is_active": "on",
            "preferred_currency": "USD",
        }
        data.update(overrides)
        self.client.force_login(self.user)
        return self.client.post(reverse("profile"), data)

    def _post_answers(self, **overrides):
        data = {
            "full_name": "", "phone": "", "linkedin_url": "", "github_url": "",
            "portfolio_url": "", "current_employer": "", "location_city": "",
            "location_country": "", "mailing_address": "", "working_timezone": "",
            "visa_rows_present": "1", "citizenship_rows_present": "1",
        }
        data.update(overrides)
        self.client.force_login(self.user)
        return self.client.post(reverse("profile_answers"), data)

    def test_search_save_stamps_changed_search_fields_as_user(self):
        self._post_search(target_tags="python, django")
        self.profile.refresh_from_db()
        self.assertEqual(set(self.profile.field_provenance), {"target_tags"})

    def test_answers_save_stamps_changed_contact_fields_as_user(self):
        self._post_answers(phone="555-0100", location_country="IND")
        self.profile.refresh_from_db()
        self.assertEqual(set(self.profile.field_provenance), {"phone", "location_country"})

    def test_untouched_resave_adds_no_new_entries_and_keeps_old_ones(self):
        self._post_answers(phone="555-0100")
        first = dict(self.profile.__class__.objects.get(pk=self.profile.pk).field_provenance)
        self._post_answers(phone="555-0100")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.field_provenance, first)

    def test_visa_repeater_row_is_stamped_per_country(self):
        self._post_answers(**{"visa_country[]": ["USA"], "visa_status[]": ["h1b"]})
        self.profile.refresh_from_db()
        self.assertIn("visa_status_by_country.USA", self.profile.field_provenance)

    def test_a_search_save_triggers_exactly_one_rematch(self):
        self._post_search(target_tags="python")
        self.assertEqual(self.mock_schedule.call_count, 1)

    def test_an_answers_save_triggers_no_rematch(self):
        self._post_answers(phone="555-0100", **{"visa_country[]": ["USA"], "visa_status[]": ["citizen"]})
        self.assertEqual(self.mock_schedule.call_count, 0)

    def test_user_edit_then_import_is_kept(self):
        self._post_answers(portfolio_url="https://me.dev")
        self.profile.refresh_from_db()
        self.assertFalse(
            profile_fields.apply_profile_field(
                self.profile, "portfolio_url", "https://other.dev", "imported"
            )
        )
