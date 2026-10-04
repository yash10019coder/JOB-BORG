import ast
from pathlib import Path

from django.test import SimpleTestCase

from apps.accounts import question_semantics as qs
from apps.accounts.question_semantics import AuthKind, ContactKind


class AuthorizationKindTests(SimpleTestCase):
    def test_authorization_and_sponsorship_are_told_apart(self):
        self.assertEqual(
            qs.authorization_kind("Are you legally authorized to work in the United States?"),
            AuthKind.AUTHORIZATION,
        )
        self.assertEqual(
            qs.authorization_kind("Will you now or in the future require visa sponsorship?"),
            AuthKind.SPONSORSHIP,
        )

    def test_combined_questions_are_ambiguous(self):
        self.assertEqual(
            qs.authorization_kind(
                "Are you authorized to work in the US, and will you require sponsorship?"
            ),
            AuthKind.AMBIGUOUS,
        )

    def test_status_questions_are_ambiguous(self):
        for text in (
            "What is your current immigration status?",
            "Are you on an H-1B?",
            "Do you hold a work permit?",
            "What is your citizenship?",
        ):
            self.assertEqual(qs.authorization_kind(text), AuthKind.AMBIGUOUS, text)

    def test_negated_phrasings_are_ambiguous(self):
        for text in (
            "Can you work without sponsorship?",
            "Do you not require visa sponsorship?",
            "Are you not authorized to work in the UK?",
        ):
            self.assertEqual(qs.authorization_kind(text), AuthKind.AMBIGUOUS, text)

    def test_unrelated_questions_are_none(self):
        self.assertEqual(qs.authorization_kind("Why do you want this job?"), AuthKind.NONE)
        self.assertEqual(qs.authorization_kind(None), AuthKind.NONE)

    def test_restriction_qualifier(self):
        for text in (
            "Are you authorized to work in the US without restrictions?",
            "Are you authorized to work for any employer?",
            "Do you have unrestricted work authorization?",
        ):
            self.assertTrue(qs.has_restriction_qualifier(text), text)
        self.assertFalse(
            qs.has_restriction_qualifier("Are you authorized to work in the US?")
        )


class CountryMentionsTests(SimpleTestCase):
    def assertMentions(self, text, alpha3=(), unresolved=False):
        found = qs.country_mentions(text)
        self.assertEqual(
            (set(found.alpha3), found.unresolved), (set(alpha3), unresolved), text
        )

    def test_a_single_named_country(self):
        self.assertMentions("Are you authorized to work in the United States?", {"USA"})
        self.assertMentions("Are you based in the U.S.?", {"USA"})
        self.assertMentions("Do you live in India?", {"IND"})
        self.assertMentions("Eligible to work in the UK?", {"GBR"})

    def test_trailing_words_are_trimmed_from_the_right(self):
        self.assertMentions(
            "Are you authorized to work in the United States for our Company?", {"USA"}
        )
        self.assertMentions("Can you work in the US without sponsorship?", {"USA"})

    def test_lowercase_words_are_never_countries(self):
        self.assertMentions("Would you work for us?", ())
        self.assertMentions("Is there anything you want us to know?", ())
        self.assertMentions("Do you work in the country where the job is located?", ())

    def test_acronyms_that_are_not_countries_are_unresolved_not_ignored(self):
        self.assertMentions("Do you have experience in IT?", (), True)
        self.assertMentions("Are you authorized to work in KSA?", (), True)

    def test_states_and_cities_are_unresolved(self):
        self.assertMentions("Are you willing to work in New Jersey?", (), True)
        self.assertMentions("Do you live in Austin?", (), True)

    def test_lists_resolve_every_member(self):
        self.assertMentions("Are you located in the US and Canada?", {"USA", "CAN"})
        self.assertMentions(
            "Are you based in Argentina/Uruguay/Chile?", {"ARG", "URY", "CHL"}
        )
        self.assertMentions(
            "Are you located in Argentina, Uruguay or Chile?", {"ARG", "URY", "CHL"}
        )

    def test_country_names_containing_and_are_not_split(self):
        self.assertMentions("Do you live in Trinidad and Tobago?", {"TTO"})

    def test_a_list_with_an_unresolvable_member_flags_it(self):
        self.assertMentions("Are you in the US or Pune?", {"USA"}, True)

    def test_two_countries_are_ambiguous(self):
        found = qs.country_mentions("Are you authorized to work in the US and Canada?")
        self.assertTrue(found.ambiguous)
        self.assertIsNone(found.single)

    def test_single_requires_exactly_one_resolved_country(self):
        self.assertEqual(qs.country_mentions("Work in India?").single, "IND")
        self.assertIsNone(qs.country_mentions("Work in India or Pune?").single)
        self.assertIsNone(qs.country_mentions("Why us?").single)

    def test_abbreviations_resolve_through_the_engine(self):
        from apps.locations.engine import alpha3_for_country

        for short, alpha3 in qs._ABBREVIATIONS.items():
            self.assertEqual(alpha3_for_country(short), alpha3, short)


class CitizenshipQuestionTests(SimpleTestCase):
    def test_citizen_of_a_country(self):
        found = qs.citizenship_question("Are you a citizen of India?")
        self.assertEqual((found.kind, found.countries, found.unresolved), ("is_citizen", ("IND",), False))

    def test_us_citizen_shorthand(self):
        found = qs.citizenship_question("Are you a US citizen?")
        self.assertEqual((found.kind, found.countries), ("is_citizen", ("USA",)))
        found = qs.citizenship_question("Are you a U.S. citizen?")
        self.assertEqual(found.countries, ("USA",))

    def test_demonym_we_cannot_resolve_is_flagged(self):
        found = qs.citizenship_question("Are you a Canadian citizen?")
        self.assertEqual(found.kind, "is_citizen")
        self.assertTrue(found.unresolved)
        self.assertEqual(found.countries, ())

    def test_nationality_selects(self):
        for text in ("Please indicate your nationality:", "Nationality", "Country of citizenship"):
            self.assertEqual(qs.citizenship_question(text).kind, "nationality", text)

    def test_status_and_permit_questions_are_not_citizenship(self):
        for text in (
            "What is your citizenship status?",
            "Are you authorized to work in the US?",
            "Will you require visa sponsorship?",
            "Do you hold a work permit?",
        ):
            self.assertIsNone(qs.citizenship_question(text), text)

    def test_unrelated_is_none(self):
        self.assertIsNone(qs.citizenship_question("Are you a morning person?"))


class ContactKindTests(SimpleTestCase):
    def test_table(self):
        table = {
            "Country": ContactKind.COUNTRY,
            "Country *": ContactKind.COUNTRY,
            "Country of residence": ContactKind.COUNTRY,
            "Which country are you currently located in?": ContactKind.COUNTRY,
            "In which country do you currently work?": ContactKind.COUNTRY,
            "Location (City)": ContactKind.CITY,
            "City": ContactKind.CITY,
            "Address": ContactKind.ADDRESS,
            "Mailing address": ContactKind.ADDRESS,
            "Please provide your home address": ContactKind.ADDRESS,
            "What time zone are you in?": ContactKind.TIMEZONE,
            "Time zone": ContactKind.TIMEZONE,
            "Location": ContactKind.LOCATION,
            "Where are you based?": ContactKind.LOCATION,
            "Where do you currently live?": ContactKind.LOCATION,
            "Are you currently located in the US?": ContactKind.MEMBERSHIP,
            "Are you based in Argentina/Uruguay?": ContactKind.MEMBERSHIP,
        }
        for text, expected in table.items():
            self.assertEqual(qs.contact_kind(text), expected, text)

    def test_things_we_do_not_answer(self):
        for text in (
            "City and State",
            "Street address line 1",
            "Street address line 2",
            "Postal code",
            "Email address",
            "What is your email address?",
            "State",
            "Are you willing to relocate to another country?",
            "What location are you applying for?",
            "Do you prefer to work in an office?",
            "Are you willing to commute to the office?",
            "Why do you want this job?",
        ):
            self.assertIsNone(qs.contact_kind(text), text)


class SalaryExpectationTests(SimpleTestCase):
    def test_expectation_wordings(self):
        for text in (
            "What is your desired salary?",
            "What are your salary expectations?",
            "What is your desired pay for this role?",
        ):
            self.assertTrue(qs.is_salary_expectation(text), text)

    def test_current_and_previous_pay_are_not_an_expectation(self):
        for text in (
            "What is your current salary?",
            "What was your previous compensation?",
            "What is your last salary?",
        ):
            self.assertFalse(qs.is_salary_expectation(text), text)

    def test_yes_no_phrasings_are_not_answered_with_a_band(self):
        self.assertFalse(
            qs.is_salary_expectation("Are you comfortable with a salary of $100k?")
        )

    def test_unrelated_is_false(self):
        self.assertFalse(qs.is_salary_expectation("Why do you want this job?"))


class LocationSensitivityTests(SimpleTestCase):
    def test_sensitive_wordings(self):
        for text in (
            "Are you willing to relocate?",
            "Are you open to working onsite five days a week?",
            "Can you work on-site in Austin?",
            "This is a hybrid role. Does that work for you?",
            "Are you able to commute to our office?",
            "Are you authorized to work in the United States?",
        ):
            self.assertTrue(qs.is_location_sensitive(text), text)

    def test_unrelated_questions_are_not(self):
        for text in (
            "How many years of Python experience do you have?",
            "Do you have experience in Python and Go?",
            "Describe your experience with Kubernetes",
            "How did you hear about us?",
            "What is your desired salary?",
        ):
            self.assertFalse(qs.is_location_sensitive(text), text)


class OptionPickerTests(SimpleTestCase):
    def test_yes_no_with_plain_options(self):
        self.assertEqual(qs.pick_yes_no(["Yes", "No"], True), "Yes")
        self.assertEqual(qs.pick_yes_no(["Yes", "No"], False), "No")

    def test_yes_no_with_sentence_options(self):
        options = ["Yes, I am authorized", "No, I am not authorized"]
        self.assertEqual(qs.pick_yes_no(options, True), "Yes, I am authorized")
        self.assertEqual(qs.pick_yes_no(options, False), "No, I am not authorized")

    def test_two_options_of_the_same_polarity_are_not_guessed(self):
        options = ["No, I require sponsorship", "No, but I do not require sponsorship"]
        self.assertIsNone(qs.pick_yes_no(options, False))

    def test_no_option_of_the_polarity(self):
        self.assertIsNone(qs.pick_yes_no(["Yes", "Prefer not to say"], False))

    def test_free_text_gets_the_literal_word(self):
        self.assertEqual(qs.pick_yes_no([], True), "Yes")
        self.assertEqual(qs.pick_yes_no((), False), "No")

    def test_not_sure_is_not_a_no(self):
        self.assertIsNone(qs.pick_yes_no(["Not sure", "Maybe"], False))

    def test_pick_exact_ignores_case_and_punctuation(self):
        self.assertEqual(qs.pick_exact(["Asia/Kolkata", "UTC"], "asia kolkata"), "Asia/Kolkata")
        self.assertIsNone(qs.pick_exact(["A", "a"], "a"))
        self.assertIsNone(qs.pick_exact(["A"], ""))

    def test_country_option_with_dial_codes(self):
        options = ["United States +1", "India +91", "British Indian Ocean Territory +246"]
        self.assertEqual(qs.pick_country_option(options, "IND"), "India +91")
        self.assertEqual(qs.pick_country_option(options, "USA"), "United States +1")
        self.assertEqual(
            qs.pick_country_option(options, "IOT"), "British Indian Ocean Territory +246"
        )

    def test_country_option_strips_parentheticals(self):
        self.assertEqual(qs.pick_country_option(["India (Republic)", "Chile"], "IND"), "India (Republic)")

    def test_country_option_missing_or_unrecognised(self):
        self.assertIsNone(qs.pick_country_option(["Atlantis +999"], "IND"))
        self.assertIsNone(qs.pick_country_option([], "IND"))

    def test_option_dial_code(self):
        self.assertEqual(qs.option_dial_code("India +91"), "91")
        self.assertEqual(qs.option_dial_code("United States +1"), "1")
        self.assertEqual(qs.option_dial_code("Trinidad and Tobago +1 868"), "1868")
        self.assertIsNone(qs.option_dial_code("India"))


class LeafModuleTests(SimpleTestCase):
    def test_imports_nothing_from_the_database_or_other_apps(self):
        tree = ast.parse(Path(qs.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        allowed_apps = {"apps.accounts.tiering", "apps.locations.engine"}
        offenders = {
            m for m in imported
            if (m.startswith("apps.") and m not in allowed_apps) or m.startswith("django")
        }
        self.assertEqual(offenders, set())
