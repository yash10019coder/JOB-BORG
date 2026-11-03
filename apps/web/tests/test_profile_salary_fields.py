"""Regressions from review of the salary-by-region fields."""
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Profile
from apps.accounts.salary_bands import validate_salary_by_region
from apps.web.forms_profile import AnswersSettingsForm

User = get_user_model()

_BASE = {
    "full_name": "", "headline": "", "target_titles": "", "target_tags": "",
    "target_locations": "", "excluded_employers": "",
    "remote_pref": Profile.RemotePref.ANY, "is_active": "on", "preferred_currency": "USD",
}


def _post(profile, **extra):
    data = QueryDict(mutable=True)
    for key, value in {**_BASE, **extra}.items():
        data.setlist(key, value if isinstance(value, list) else [value])
    form = AnswersSettingsForm(data, instance=profile)
    assert form.is_valid(), form.errors
    form.save()
    profile.refresh_from_db()


class SalaryByRegionPostTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="keep", password="pw").profile
        self.profile.salary_by_region = {"US": "175-200k", "UK": "80-100k"}
        self.profile.save()

    def test_a_post_without_the_salary_selects_keeps_the_stored_bands(self):
        _post(self.profile)
        self.assertEqual(self.profile.salary_by_region, {"US": "175-200k", "UK": "80-100k"})

    def test_a_posted_blank_select_clears_only_that_region(self):
        _post(self.profile, salary_region_US="")
        self.assertEqual(self.profile.salary_by_region, {"UK": "80-100k"})


class SalaryBlankKeyTests(SimpleTestCase):
    def test_blank_band_key_is_not_a_selection(self):
        self.assertEqual(validate_salary_by_region({"US": ""}), {})
        self.assertEqual(validate_salary_by_region({"US": "175-200k"}), {"US": "175-200k"})
