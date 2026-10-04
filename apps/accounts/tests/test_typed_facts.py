from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.services import typed_facts

User = get_user_model()

YES_NO = ("Yes", "No")
AUTH_Q = "Are you legally authorized to work in the country where this job is located?"
AUTH_US_Q = "Are you legally authorized to work in the United States?"
SPONSOR_Q = "Will you now or in the future require visa sponsorship?"


def job(country="US", employer="Acme"):
    return SimpleNamespace(location_country=country, employer=SimpleNamespace(name=employer))


class _Base(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def set(self, **fields):
        for name, value in fields.items():
            setattr(self.profile, name, value)
        self.profile.save()


class AuthorizationTests(_Base):
    def test_citizen_of_the_jobs_country_is_authorized_and_needs_no_sponsorship(self):
        self.set(citizenship_countries=["IND"])
        fact = typed_facts.resolve(self.profile, AUTH_Q, options=YES_NO, job=job("India"))
        self.assertEqual((fact.value, fact.covered, fact.truth), ("Yes", True, True))
        self.assertEqual(fact.provenance_key, "citizenship_countries")
        sponsor = typed_facts.resolve(self.profile, SPONSOR_Q, options=YES_NO, job=job("India"))
        self.assertEqual((sponsor.value, sponsor.truth), ("No", False))

    def test_the_country_named_in_the_question_beats_the_jobs_country(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(self.profile, AUTH_US_Q, options=YES_NO, job=job("India"))
        self.assertEqual(fact.value, "Yes")
        self.assertEqual(fact.provenance_key, "visa_status_by_country.USA")
        self.assertEqual(fact.detail["country"], "USA")

    def test_h1b_is_authorized_but_sponsorship_stays_blank(self):
        self.set(visa_status_by_country={"USA": "h1b"})
        auth = typed_facts.resolve(self.profile, AUTH_US_Q, options=YES_NO)
        sponsor = typed_facts.resolve(self.profile, SPONSOR_Q, options=YES_NO, job=job("US"))
        self.assertEqual(auth.value, "Yes")
        self.assertIsNone(sponsor.value)
        self.assertEqual((sponsor.covered, sponsor.reason), (True, "status_unknown"))

    def test_a_restriction_qualifier_blanks_a_visa_holders_authorization(self):
        self.set(visa_status_by_country={"USA": "h1b"})
        fact = typed_facts.resolve(
            self.profile,
            "Are you authorized to work in the United States without restrictions?",
            options=YES_NO,
        )
        self.assertIsNone(fact.value)
        self.assertEqual((fact.covered, fact.reason), (True, "restricted_authorization"))

    def test_a_restriction_qualifier_does_not_blank_a_citizen(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(
            self.profile,
            "Are you authorized to work in the United States without restrictions?",
            options=YES_NO,
        )
        self.assertEqual(fact.value, "Yes")

    def test_requires_sponsorship_status(self):
        self.set(visa_status_by_country={"USA": "requires_sponsorship"})
        sponsor = typed_facts.resolve(self.profile, SPONSOR_Q, options=YES_NO, job=job("US"))
        auth = typed_facts.resolve(self.profile, AUTH_US_Q, options=YES_NO)
        self.assertEqual(sponsor.value, "Yes")
        self.assertIsNone(auth.value)  # unknown: they may be authorized for a current employer

    def test_not_authorized_answers_no_but_not_sponsorship(self):
        self.set(visa_status_by_country={"USA": "not_authorized"})
        self.assertEqual(typed_facts.resolve(self.profile, AUTH_US_Q, options=YES_NO).value, "No")
        self.assertIsNone(
            typed_facts.resolve(self.profile, SPONSOR_Q, options=YES_NO, job=job("US")).value
        )

    def test_no_entry_for_the_country_is_not_covered(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(self.profile, AUTH_Q, options=YES_NO, job=job("Canada"))
        self.assertEqual((fact.value, fact.covered, fact.reason), (None, False, "no_country_entry"))

    def test_no_country_anywhere(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(self.profile, AUTH_Q, options=YES_NO)
        self.assertEqual((fact.covered, fact.reason), (False, "no_country"))
        fact = typed_facts.resolve(self.profile, AUTH_Q, options=YES_NO, job=job(""))
        self.assertEqual(fact.reason, "no_country")

    def test_an_unresolvable_named_place_does_not_fall_back_to_the_jobs_country(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(
            self.profile, "Are you authorized to work in KSA?", options=YES_NO, job=job("US")
        )
        self.assertEqual((fact.value, fact.reason), (None, "country_unresolved"))

    def test_two_named_countries_are_not_answered(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        fact = typed_facts.resolve(
            self.profile, "Are you authorized to work in the US and Canada?", options=YES_NO
        )
        self.assertEqual((fact.value, fact.reason), (None, "multiple_countries"))

    def test_options_without_an_unambiguous_yes_no_are_not_guessed(self):
        self.set(visa_status_by_country={"USA": "requires_sponsorship"})
        fact = typed_facts.resolve(
            self.profile,
            SPONSOR_Q,
            options=("No, I require sponsorship", "No, but I do not require sponsorship"),
            job=job("US"),
        )
        self.assertEqual((fact.value, fact.reason), (None, "no_option_match"))

    def test_free_text_gets_the_literal_word(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        self.assertEqual(typed_facts.resolve(self.profile, AUTH_US_Q).value, "Yes")

    def test_status_and_combined_questions_are_not_typed_questions(self):
        self.set(visa_status_by_country={"USA": "citizen"})
        for text in (
            "What is your current immigration status?",
            "Are you authorized to work in the US, and will you require sponsorship?",
            "Can you work in the US without sponsorship?",
        ):
            self.assertIsNone(typed_facts.resolve(self.profile, text, options=YES_NO), text)

    def test_unrelated_question_is_not_a_typed_question(self):
        self.assertIsNone(typed_facts.resolve(self.profile, "Why do you want this job?"))
        self.assertIsNone(typed_facts.resolve(None, AUTH_US_Q))


class CitizenshipTests(_Base):
    def test_is_a_citizen_of_a_held_country(self):
        self.set(citizenship_countries=["IND"])
        fact = typed_facts.resolve(self.profile, "Are you a citizen of India?", options=YES_NO)
        self.assertEqual((fact.value, fact.truth), ("Yes", True))

    def test_not_a_citizen_of_another_country_when_the_list_is_maintained(self):
        self.set(citizenship_countries=["IND"])
        fact = typed_facts.resolve(self.profile, "Are you a U.S. citizen?", options=YES_NO)
        self.assertEqual((fact.value, fact.truth), ("No", False))

    def test_an_empty_list_answers_nothing(self):
        fact = typed_facts.resolve(self.profile, "Are you a US citizen?", options=YES_NO)
        self.assertEqual((fact.value, fact.covered, fact.reason), (None, False, "no_citizenship_entered"))

    def test_unresolvable_demonym_is_not_answered(self):
        self.set(citizenship_countries=["CAN"])
        fact = typed_facts.resolve(self.profile, "Are you a Canadian citizen?", options=YES_NO)
        self.assertEqual((fact.value, fact.reason), (None, "country_unresolved"))

    def test_nationality_select_with_one_citizenship(self):
        self.set(citizenship_countries=["IND"])
        fact = typed_facts.resolve(
            self.profile, "Please indicate your nationality:", options=("Chile", "India", "Peru")
        )
        self.assertEqual(fact.value, "India")

    def test_nationality_select_with_two_citizenships_is_not_guessed(self):
        self.set(citizenship_countries=["IND", "USA"])
        fact = typed_facts.resolve(
            self.profile, "Nationality", options=("India", "United States")
        )
        self.assertEqual((fact.value, fact.reason), (None, "multiple_citizenships"))

    def test_nationality_free_text_uses_the_country_label(self):
        self.set(citizenship_countries=["IND"])
        self.assertEqual(typed_facts.resolve(self.profile, "Nationality").value, "India")

    def test_status_question_is_not_a_citizenship_question(self):
        self.set(citizenship_countries=["IND"])
        self.assertIsNone(typed_facts.resolve(self.profile, "What is your citizenship status?"))


class SalaryTests(_Base):
    BANDS = {"US": "100-125k", "IN": "20-30l"}
    SALARY_Q = "What is your desired salary?"

    def _band_label(self, region, band):
        from apps.accounts.salary_bands import get_salary_band_label

        return get_salary_band_label(region, band)

    def test_us_job_gets_the_us_band(self):
        self.set(salary_by_region={"US": "100-125k"})
        fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job("US"))
        self.assertEqual(fact.value, self._band_label("US", "100-125k"))
        self.assertEqual(fact.provenance_key, "salary_by_region.US")
        self.assertEqual(fact.detail, {"region": "US", "band": "100-125k"})

    def test_option_matching_is_exact_on_the_band_label(self):
        self.set(salary_by_region={"US": "100-125k"})
        label = self._band_label("US", "100-125k")
        hit = typed_facts.resolve(self.profile, self.SALARY_Q, options=("x", label), job=job("US"))
        miss = typed_facts.resolve(self.profile, self.SALARY_Q, options=("a", "b"), job=job("US"))
        self.assertEqual(hit.value, label)
        self.assertEqual((miss.value, miss.covered, miss.reason), (None, True, "no_option_match"))

    def test_region_without_a_band_is_not_covered(self):
        self.set(salary_by_region={"US": "100-125k"})
        fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job("India"))
        self.assertEqual((fact.value, fact.covered, fact.reason), (None, False, "no_band"))

    def test_job_without_a_country_or_with_no_region_is_not_covered(self):
        self.set(salary_by_region={"US": "100-125k"})
        for country in ("", "Japan"):
            fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job(country))
            self.assertEqual((fact.value, fact.reason), (None, "no_region"), country)
        self.assertEqual(typed_facts.resolve(self.profile, self.SALARY_Q).reason, "no_region")

    def test_the_other_band_is_not_an_answer(self):
        self.set(salary_by_region={"US": "other"})
        fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job("US"))
        self.assertEqual((fact.value, fact.reason), (None, "no_band"))

    def test_min_salary_is_never_converted_into_an_answer(self):
        self.set(min_salary=2_000_000, preferred_currency="INR")
        fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job("US"))
        self.assertIsNone(fact.value)

    def test_current_salary_and_yes_no_phrasings_are_not_salary_facts(self):
        self.set(salary_by_region={"US": "100-125k"})
        for text in (
            "What is your current salary?",
            "Are you comfortable with a salary of $100k?",
        ):
            self.assertIsNone(typed_facts.resolve(self.profile, text, job=job("US")), text)

    def test_stale_or_invalid_stored_band_is_ignored(self):
        self.set(salary_by_region={"US": "no-such-band", "XX": "1-2k"})
        fact = typed_facts.resolve(self.profile, self.SALARY_Q, job=job("US"))
        self.assertEqual((fact.value, fact.reason), (None, "no_band"))


class ContactTests(_Base):
    DIAL_OPTIONS = ("United States +1", "India +91", "British Indian Ocean Territory +246")

    def test_country_picker_with_dial_codes(self):
        self.set(location_country="IND")
        fact = typed_facts.resolve(self.profile, "Country", options=self.DIAL_OPTIONS)
        self.assertEqual(fact.value, "India +91")
        self.assertEqual(fact.provenance_key, "location_country")

    def test_country_free_text_uses_the_label(self):
        self.set(location_country="IND")
        self.assertEqual(typed_facts.resolve(self.profile, "Country *").value, "India")

    def test_a_dial_code_that_contradicts_the_phone_is_not_answered(self):
        self.set(location_country="USA", phone="+91 98765 43210")
        fact = typed_facts.resolve(self.profile, "Country", options=self.DIAL_OPTIONS)
        self.assertEqual((fact.value, fact.reason), (None, "phone_dial_mismatch"))

    def test_a_matching_or_local_phone_is_fine(self):
        self.set(location_country="IND", phone="+91 98765 43210")
        self.assertEqual(
            typed_facts.resolve(self.profile, "Country", options=self.DIAL_OPTIONS).value, "India +91"
        )
        self.set(phone="098765 43210")
        self.assertEqual(
            typed_facts.resolve(self.profile, "Country", options=self.DIAL_OPTIONS).value, "India +91"
        )

    def test_country_not_in_the_options(self):
        self.set(location_country="IND")
        fact = typed_facts.resolve(self.profile, "Country", options=("Chile +56", "Peru +51"))
        self.assertEqual((fact.value, fact.covered, fact.reason), (None, True, "no_option_match"))

    def test_nothing_entered(self):
        for text in ("Country", "Location (City)", "Address", "What time zone are you in?", "Location"):
            fact = typed_facts.resolve(self.profile, text)
            self.assertEqual((fact.value, fact.covered, fact.reason), (None, False, "not_entered"), text)

    def test_city_address_location(self):
        self.set(location_city="Pune", location_country="IND", mailing_address="12 MG Road\nPune 411001\n")
        self.assertEqual(typed_facts.resolve(self.profile, "Location (City)").value, "Pune")
        self.assertEqual(
            typed_facts.resolve(self.profile, "Mailing address").value, "12 MG Road, Pune 411001"
        )
        self.assertEqual(typed_facts.resolve(self.profile, "Where are you based?").value, "Pune, India")

    def test_location_needs_both_city_and_country(self):
        self.set(location_country="IND")
        self.assertEqual(typed_facts.resolve(self.profile, "Location").reason, "not_entered")

    def test_timezone_free_text_names_the_zone_and_offset(self):
        self.set(working_timezone="Asia/Kolkata")
        value = typed_facts.resolve(self.profile, "What time zone are you in?").value
        self.assertEqual(value, "Asia/Kolkata (UTC+05:30)")

    def test_timezone_option_by_iana_name_or_offset(self):
        self.set(working_timezone="Asia/Kolkata")
        by_name = typed_facts.resolve(
            self.profile, "Time zone", options=("America/New_York", "Asia/Kolkata")
        )
        self.assertEqual(by_name.value, "Asia/Kolkata")
        by_offset = typed_facts.resolve(
            self.profile, "Time zone", options=("(UTC-05:00) Eastern", "(UTC+05:30) India Standard")
        )
        self.assertEqual(by_offset.value, "(UTC+05:30) India Standard")

    def test_timezone_with_no_matching_option(self):
        self.set(working_timezone="Asia/Kolkata")
        fact = typed_facts.resolve(self.profile, "Time zone", options=("Pacific", "Eastern"))
        self.assertEqual((fact.value, fact.reason), (None, "no_option_match"))

    def test_membership_yes_no(self):
        self.set(location_country="USA")
        yes = typed_facts.resolve(self.profile, "Are you currently located in the US?", options=YES_NO)
        no = typed_facts.resolve(
            self.profile, "Are you currently located in Argentina, Uruguay or Chile?", options=YES_NO
        )
        self.assertEqual((yes.value, yes.truth), ("Yes", True))
        self.assertEqual((no.value, no.truth), ("No", False))

    def test_membership_with_a_city_level_place_is_not_answered(self):
        self.set(location_country="USA")
        fact = typed_facts.resolve(
            self.profile, "Are you currently located in the Bay Area?", options=YES_NO
        )
        self.assertEqual((fact.value, fact.covered, fact.reason), (None, True, "place_unresolved"))

    def test_membership_without_a_location_is_not_covered(self):
        fact = typed_facts.resolve(self.profile, "Are you based in the US?", options=YES_NO)
        self.assertEqual((fact.value, fact.covered), (None, False))

    def test_things_we_do_not_answer_are_not_typed(self):
        self.set(location_country="IND", location_city="Pune")
        for text in ("City and State", "Postal code", "Street address line 1", "Email address"):
            self.assertIsNone(typed_facts.resolve(self.profile, text), text)
