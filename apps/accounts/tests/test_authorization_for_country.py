from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Profile

User = get_user_model()


class AuthorizationForCountryTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def _with(self, status, country="USA"):
        self.profile.visa_status_by_country = {country: status}
        return self.profile.authorization_for_country(country)

    def test_every_visa_status_has_a_defined_pair(self):
        expected = {
            "citizen": (True, False),
            "permanent_resident": (True, False),
            "work_permit": (True, False),
            "requires_sponsorship": (None, True),
            "not_authorized": (False, None),
            "h1b": (True, None),
            "opt": (True, None),
            "o1": (True, None),
            "tn": (True, None),
            "e3": (True, None),
            "other": (None, None),
        }
        self.assertEqual({v for v, _ in Profile.VisaStatus.choices}, set(expected))
        for status, pair in expected.items():
            self.assertEqual(self._with(status), pair, status)

    def test_a_work_visa_never_decides_sponsorship(self):
        for status in ("h1b", "opt", "o1", "tn", "e3", "other"):
            self.assertIsNone(self._with(status)[1], status)

    def test_not_authorized_does_not_claim_no_sponsorship_is_needed(self):
        authorized, needs_sponsorship = self._with("not_authorized")
        self.assertFalse(authorized)
        self.assertIsNone(needs_sponsorship)

    def test_no_entry_for_the_country_is_none_not_a_pair(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.assertIsNone(self.profile.authorization_for_country("India"))
        self.assertIsNone(self.profile.authorization_for_country("IND"))

    def test_citizenship_is_authoritative_for_its_own_country(self):
        self.profile.citizenship_countries = ["IND"]
        self.profile.visa_status_by_country = {"IND": "not_authorized"}
        self.assertEqual(self.profile.authorization_for_country("India"), (True, False))

    def test_country_accepted_in_every_form(self):
        self.profile.visa_status_by_country = {"DEU": "work_permit"}
        for form in ("DEU", "Germany"):
            self.assertEqual(self.profile.authorization_for_country(form), (True, False), form)

    def test_unknown_or_blank_country_is_none(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        for value in ("", "Atlantis", None):
            self.assertIsNone(self.profile.authorization_for_country(value), value)

    def test_state_colliding_alpha2_codes_do_not_resolve(self):
        self.profile.visa_status_by_country = {"CAN": "citizen", "IND": "citizen", "DEU": "citizen"}
        for code in ("CA", "IN", "DE"):
            self.assertIsNone(self.profile.authorization_for_country(code), code)

    def test_legacy_unknown_status_value_is_none(self):
        self.profile.visa_status_by_country = {"USA": "cpt"}
        self.assertIsNone(self.profile.authorization_for_country("USA"))


class LocationColumnsTests(TestCase):
    def test_new_columns_default_to_blank(self):
        profile = User.objects.create_user(username="bob", password="pw").profile
        self.assertEqual(
            (profile.location_city, profile.location_country,
             profile.mailing_address, profile.working_timezone),
            ("", "", "", ""),
        )
