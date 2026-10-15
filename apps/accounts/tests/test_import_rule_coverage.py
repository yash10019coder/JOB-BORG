"""Remaining rule tests, and the guard that keeps every rule tested.

``docs/plans/2026-10-05-002-profile-import-rules.md`` is the contract for the
importer. :class:`RuleCoverageTests` fails when a rule ID in that document is not
cited (in a test name, docstring or comment) by any import test, so a rule cannot
be added or kept without a test that names it.
"""
import re
from datetime import date
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase

from apps.accounts.importing import entries as ent
from apps.accounts.importing import pipeline, rules
from apps.accounts.importing import sections as sec
from apps.accounts.importing.documents import normalize_text
from apps.accounts.importing.grounding import derive_location, derive_name
from apps.accounts.importing.validators import ImportValueError
from apps.accounts.tests import import_fixtures as fx
from apps.accounts.tests.test_import_rules import LinkedInLayoutTests

TODAY = date(2026, 10, 5)
RULES_DOC = Path(settings.BASE_DIR) / "docs/plans/2026-10-05-002-profile-import-rules.md"


def run(text):
    return ent.extract_entries(normalize_text(text), TODAY)


def resume(body, tail="Skills\nPython, Go\n"):
    return f"Jane Doe\njane@example.com\nExperience\n{body}\n{tail}"


class SectionRuleTests(SimpleTestCase):
    def test_S4_spoken_languages_interests_and_references_are_never_parsed(self):
        text = (
            "Jane Doe\nTechnical Skills\nPython\nLanguages\nEnglish, Hindi\nInterests\nGo, Rust, Kubernetes\n"
            "References\nInitech\nBackend Developer  Mar 2019 - Nov 2022\n"
        )
        normalized = normalize_text(text)
        names = [s.name for s in rules.declared_skills(rules.prepare(normalized))]
        self.assertEqual(names, ["Python"])
        self.assertEqual(run(text), [])  # a role under References is not an experience entry

    def test_S4_education_content_is_never_an_experience_entry(self):
        text = "Jane Doe\nEducation\nExample University\nSoftware Engineering Intern  Mar 2019 - Nov 2022\n"
        self.assertEqual(run(text + "x " * 150), [])

    def test_S6_a_heading_looking_line_inside_a_bullet_is_not_a_heading(self):
        for line in ("• Education", "◦ EXPERIENCE", "- Skills"):
            self.assertEqual(sec.find_sections((line, "a", "b")), [], line)

    def test_S7_only_the_first_ten_lines_are_the_contact_zone_for_location_and_portfolio(self):
        filler = "\n".join(f"line {i}" for i in range(11))
        inside = f"Jane Doe\nLucknow, India | janedoe.dev\n{filler}\n" + "x " * 150
        outside = f"Jane Doe\n{filler}\nLucknow, India | janedoe.dev\n" + "x " * 150
        found_in = {p.field for p in rules.extract_fields(normalize_text(inside))}
        found_out = {p.field for p in rules.extract_fields(normalize_text(outside))}
        self.assertTrue({"location_city", "portfolio_url"} <= found_in)
        self.assertTrue({"location_city", "portfolio_url"}.isdisjoint(found_out))

    def test_S11_linkedin_entries_use_the_dates_in_the_text_and_never_the_printed_duration(self):
        text = LinkedInLayoutTests.EXPORT + "x " * 200
        entries = [e for e in run(text) if e.kind == "experience"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry.title, entry.organization), ("Staff Software Engineer", "Acme"))
        self.assertEqual((entry.start, entry.end, entry.is_current), (date(2020, 1, 1), None, True))

    def test_EX5_a_printed_duration_never_changes_the_dates(self):
        wrong = resume("Initech\nBackend Developer  Mar 2019 - Nov 2022 (30 years 2 months)")
        right = resume("Initech\nBackend Developer  Mar 2019 - Nov 2022")
        self.assertEqual(
            [(e.start, e.end) for e in run(wrong)], [(e.start, e.end) for e in run(right)]
        )
        self.assertEqual(run(wrong)[0].start, date(2019, 3, 1))


class FieldRuleTests(SimpleTestCase):
    def test_FN6_a_name_longer_than_the_column_is_refused(self):
        long_name = " ".join(["Abcdefghij" * 8] * 4)  # 4 tokens of 80 letters
        with self.assertRaises(ImportValueError):
            derive_name(long_name)
        self.assertEqual(derive_name("Jane Doe"), "Jane Doe")

    def test_FL4_the_country_is_a_known_alpha3_code(self):
        self.assertEqual(derive_location("Lucknow, India")[1], "IND")
        self.assertEqual(derive_location("San Francisco, CA")[1], "USA")

    def test_FH1_ordinary_resumes_never_propose_a_headline(self):
        for text in (fx.BULLET_ORG_RESUME, fx.LABEL_NAME_RESUME, fx.CAPS_RESUME, fx.REPEATED_HEADER_RESUME):
            names = {p.field for p in rules.extract_fields(normalize_text(text))}
            self.assertNotIn("headline", names)

    def test_FE2_fields_derived_from_entries_exist_only_when_an_entry_is_resolved(self):
        names = {p.field for p in pipeline.run_rules(normalize_text(fx.BULLET_ORG_RESUME), TODAY).fields}
        self.assertTrue({"current_employer", "target_titles"} <= names)

    def test_FK3_the_tag_list_is_capped(self):
        original = rules.MAX_TAGS
        rules.MAX_TAGS = 2
        try:
            proposal = rules.extract_tags(rules.prepare(normalize_text("Skills\nPython, Go, Kubernetes, JavaScript\n")))
        finally:
            rules.MAX_TAGS = original
        self.assertEqual(proposal.value, ["python", "golang"])


class EntryRuleTests(SimpleTestCase):
    def test_EX6_the_header_is_the_anchor_line_without_the_range_or_trailing_separators(self):
        doc = rules.prepare(normalize_text(resume("Backend Developer at Initech –  Mar 2019 - Nov 2022")))
        anchors = ent._find_anchors(doc, TODAY)
        self.assertEqual([a.head.strip(" –-") for a in anchors], ["Backend Developer at Initech"])
        entry = run(resume("Backend Developer at Initech –  Mar 2019 - Nov 2022"))[0]
        self.assertEqual((entry.title, entry.organization), ("Backend Developer", "Initech"))

    def test_EX14_concurrent_roles_are_both_kept(self):
        entries = run(resume("Initech\nBackend Developer  Mar 2019 - Nov 2022\nHooli\nData Engineer  Jan 2020 - Present"))
        self.assertEqual({e.organization for e in entries}, {"Initech", "Hooli"})

    def test_EX14_intern_and_contract_words_in_the_title_stay_but_a_parenthetical_qualifier_goes(self):
        entries = run(resume(
            "Initech\nSoftware Engineering Intern  Mar 2019 - Nov 2019\n"
            "Hooli\nContract Backend Developer  Jan 2020 - Dec 2020\n"
            "Pied Piper\nData Engineer (Contract)  Jan 2021 - Dec 2021"
        ))
        self.assertEqual(
            sorted(e.title for e in entries),
            ["Contract Backend Developer", "Data Engineer", "Software Engineering Intern"],
        )

    def test_Z2_education_certifications_and_spoken_languages_are_not_stored(self):
        text = (
            "Jane Doe\nCertifications\nAWS Certified Solutions Architect 2021 - 2024\nCourses\nDeep Learning Course 2020 - 2021\n"
            "Languages\nEnglish, Hindi\nEducation\nB.Tech Computer Science 2013 - 2017\n" + "x " * 150
        )
        extraction = pipeline.run_rules(normalize_text(text), TODAY)
        self.assertEqual(extraction.entries, [])
        self.assertEqual({p.field for p in extraction.fields} - {"full_name"}, set())


@skipUnless(RULES_DOC.exists(), "the rules document is not shipped in this environment")
class RuleCoverageTests(SimpleTestCase):
    def cited_text(self):
        tests = Path(settings.BASE_DIR) / "apps"
        files = (
            list((tests / "accounts/tests").glob("*import*.py"))
            + list((tests / "accounts/tests").glob("*github*.py"))
            + list((tests / "web/tests").glob("test_import_views*.py"))
            + [tests / "accounts/tests/test_resume_facts.py", tests / "accounts/tests/test_profile_skills.py"]
        )
        return "\n".join(path.read_text() for path in files)

    def test_every_rule_in_the_contract_is_cited_by_a_test(self):
        ids = re.findall(r"^- \*\*([A-Z]+\d+)\*\*", RULES_DOC.read_text(), re.M)
        self.assertGreater(len(ids), 100)  # the document was actually parsed
        cited = self.cited_text()
        uncited = [i for i in ids if not re.search(rf"(?<![A-Za-z0-9]){i}(?![0-9])", cited)]
        self.assertEqual(uncited, [], "rules with no test naming them: " + ", ".join(uncited))
