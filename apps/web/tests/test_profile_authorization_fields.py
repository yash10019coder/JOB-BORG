"""Regressions from review of the per-country authorization fields."""
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import Profile
from apps.web.forms import ProfileForm
from apps.web.salary_bands import validate_salary_by_region

User = get_user_model()

_BASE = {
    "full_name": "", "headline": "", "target_titles": "", "target_tags": "",
    "target_locations": "", "excluded_employers": "",
    "remote_pref": Profile.RemotePref.ANY, "is_active": "on", "preferred_currency": "USD",
}


def _post(profile, **extra):
    data = QueryDict(mutable=True)
    for key, value in {**_BASE, **extra}.items():
        values = value if isinstance(value, list) else [value]
        data.setlist(key, values)
    form = ProfileForm(data, instance=profile)
    assert form.is_valid(), form.errors
    form.save()
    profile.refresh_from_db()


class PreserveOmittedFieldsTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="keep", password="pw").profile
        self.profile.visa_status_by_country = {"USA": "h1b"}
        self.profile.citizenship_countries = ["IND"]
        self.profile.salary_by_region = {"US": "175-200k"}
        self.profile.save()

    def test_post_without_the_authorization_section_keeps_saved_values(self):
        _post(self.profile)
        self.assertEqual(self.profile.visa_status_by_country, {"USA": "h1b"})
        self.assertEqual(self.profile.citizenship_countries, ["IND"])
        self.assertEqual(self.profile.salary_by_region, {"US": "175-200k"})

    def test_an_emptied_repeater_still_clears_when_the_section_was_posted(self):
        _post(self.profile, authorization_present="1")
        self.assertEqual(self.profile.visa_status_by_country, {})
        self.assertEqual(self.profile.citizenship_countries, [])
        self.assertEqual(self.profile.salary_by_region, {"US": "175-200k"})

    def test_posted_repeater_rows_replace_the_stored_ones(self):
        _post(self.profile, **{"visa_country[]": ["India"], "visa_status[]": ["citizen"]})
        self.assertEqual(self.profile.visa_status_by_country, {"IND": "citizen"})

    def test_a_posted_blank_salary_select_clears_only_that_region(self):
        self.profile.salary_by_region = {"US": "175-200k", "UK": "£80-100k"}
        self.profile.save()
        _post(self.profile, salary_region_US="")
        self.assertNotIn("US", self.profile.salary_by_region)
        self.assertIn("UK", self.profile.salary_by_region)


class SalaryBlankKeyTests(SimpleTestCase):
    def test_blank_band_key_is_not_a_selection(self):
        self.assertEqual(validate_salary_by_region({"US": ""}), {})
        self.assertEqual(validate_salary_by_region({"US": "175-200k"}), {"US": "175-200k"})


class NotAuthorizedMappingTests(TestCase):
    def test_not_authorized_never_reports_that_no_sponsorship_is_needed(self):
        profile = User.objects.create_user(username="na", password="pw").profile
        profile.visa_status_by_country = {"USA": "not_authorized"}
        self.assertEqual(profile.authorization_for_country("USA"), ("no_need_sponsorship", ""))

    def test_explicit_answers_autofill_agrees(self):
        from apps.web.forms import ExplicitAnswersForm

        for value in ({"USA": "not_authorized"}, "not_authorized"):
            self.assertEqual(ExplicitAnswersForm._visa_status_to_auth(value), "no_need_sponsorship")
            self.assertEqual(ExplicitAnswersForm._visa_status_to_sponsorship(value), "")


class InvalidExplicitAnswersPostTests(TestCase):
    def test_invalid_post_keeps_the_submitted_values_and_errors(self):
        user = User.objects.create_user(username="bound", password="pw")
        self.client.force_login(user)
        response = self.client.post(
            reverse("explicit_answers"), {"work_authorization": "not-a-choice", "sponsorship": "no"}
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["explicit_answers_form"]
        self.assertTrue(form.is_bound)
        self.assertIn("work_authorization", form.errors)
        self.assertEqual(form.data["sponsorship"], "no")
