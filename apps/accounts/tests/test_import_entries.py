"""Experience and project entries (rules EX1-EX16, EA1-EA5, FE1, FT1, G2)."""
import dataclasses
import time
from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.importing import entries as ent
from apps.accounts.importing import pipeline
from apps.accounts.importing.documents import normalize_text
from apps.accounts.models import ResumeEntry
from apps.accounts.services.resume_facts import apply_entries
from apps.accounts.tests import import_fixtures as fx

TODAY = date(2026, 10, 5)
FILLER = "\n" + "Languages: Python, SQL\n" * 0  # keeps fixtures readable


def run(text, today=TODAY):
    return ent.extract_entries(normalize_text(text), today)


def exp(text):
    return [e for e in run(text) if e.kind == "experience"]


def resume(body, *, skills="Python, Go, Django, AWS, Kubernetes, PostgreSQL"):
    return f"Jane Doe\njane@example.com\nExperience\n{body}\nEducation\nB.Tech Computer Science 2012 - 2016\nSkills\n{skills}\n"


class DateTests(SimpleTestCase):
    def test_EX1_date_forms(self):
        cases = {
            "Jan 2020": (date(2020, 1, 1), "month"), "January 2020": (date(2020, 1, 1), "month"),
            "Sept 2021": (date(2021, 9, 1), "month"), "Sep. 2021": (date(2021, 9, 1), "month"),
            "03/2019": (date(2019, 3, 1), "month"), "2019-03": (date(2019, 3, 1), "month"),
            "15 Mar 2020": (date(2020, 3, 1), "month"), "Mar '19": (date(2019, 3, 1), "month"),
            "Mar ’99": (date(1999, 3, 1), "month"), "2018": (date(2018, 1, 1), "year"),
        }
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(ent.parse_date(token), expected)

    def test_EX2_a_year_only_end_is_december(self):
        self.assertEqual(ent.parse_date("2018", end=True), (date(2018, 12, 1), "year"))

    def test_EX1_invalid_dates_do_not_parse(self):
        for token in ("13/2020", "2020-13", "Foo 2020", "Mar 20", "", "20"):
            with self.subTest(token=token):
                self.assertIsNone(ent.parse_date(token))

    def test_EX1_ranges_with_every_separator_and_end_word(self):
        for text in ("Jan 2020 – Mar 2021", "Jan 2020 — Mar 2021", "Jan 2020 - Mar 2021",
                     "Jan 2020-Mar 2021", "Jan 2020 to Mar 2021", "Jan 2020 until Mar 2021"):
            with self.subTest(text=text):
                start, end, current, precision, ok = ent.parse_range(text, TODAY)
                self.assertEqual((start, end, current, precision, ok), (date(2020, 1, 1), date(2021, 3, 1), False, "month", True))
        for word in ("Present", "current", "Now", "ongoing", "till date", "To Date", "today"):
            with self.subTest(word=word):
                start, end, current, _, ok = ent.parse_range(f"Jan 2020 - {word}", TODAY)
                self.assertEqual((end, current, ok), (None, True, True))

    def test_EX2_year_only_ranges_are_flagged_year_precision(self):
        start, end, current, precision, _ = ent.parse_range("2019 - 2021", TODAY)
        self.assertEqual((start, end, precision), (date(2019, 1, 1), date(2021, 12, 1), "year"))

    def test_EX4_implausible_ranges_parse_but_are_not_plausible(self):
        for text in ("Mar 2021 - Jan 2020", "Jan 2020 - Dec 2030", "Jan 1960 - Jan 1962"):
            with self.subTest(text=text):
                self.assertFalse(ent.parse_range(text, TODAY)[4])

    def test_EX1_text_that_is_not_a_range(self):
        for text in ("Jan 2020", "Built things in 2020", "2020"):
            self.assertIsNone(ent.parse_range(text, TODAY))


class ClassifyTests(SimpleTestCase):
    def test_EX7_titles_orgs_and_places(self):
        self.assertEqual(ent.classify("Senior Backend Engineer"), ent.TITLE)
        self.assertEqual(ent.classify("Acme Payments"), ent.ORG)
        self.assertEqual(ent.classify("Pune, Maharashtra"), ent.LOC)
        self.assertEqual(ent.classify("Remote"), ent.LOC)
        self.assertEqual(ent.classify("Karnataka"), ent.LOC)

    def test_EX7_sentences_verbs_and_tech_lists_are_detail(self):
        for text in ("Built payment APIs and improved latency.", "Perform CRUD operations and manipulate data and schema",
                     "Java, SpringBoot, PgSQL, MongoDB, Python", "Led a team of five engineers", "x " * 20, "1234", ""):
            with self.subTest(text=text):
                self.assertEqual(ent.classify(text), ent.DETAIL)

    def test_EX7_a_city_alone_is_not_a_place_because_company_names_resolve_to_cities(self):
        self.assertEqual(ent.classify("Google"), ent.ORG)
        self.assertEqual(ent.classify("Phoenix"), ent.ORG)

    def test_peeling_removes_only_a_trailing_location(self):
        cases = {
            "0chain Cupertino, CA": ("0chain", False),
            "Acme Remote": ("Acme", False),
            "SDE Intern Remote": ("SDE Intern", False),
            "BSHAPP Andhra Pradesh": ("BSHAPP", False),
            "Acme (Remote)": ("Acme", False),
            "BrowserStack Mumbai": ("BrowserStack", True),
            "Gnome Foundation United States of America": ("Gnome Foundation", True),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ent._peel_location(text), expected)

    def test_peeling_never_eats_an_org_suffix_word_or_the_whole_name(self):
        for text in ("Hewlett Packard Enterprise", "Google", "Bank of India", "Phoenix", "Zeotap Labs"):
            with self.subTest(text=text):
                self.assertEqual(ent._peel_location(text), (text, False))


class LayoutTests(SimpleTestCase):
    def test_EX7_bulleted_org_line_then_title_dash_location_dates(self):
        entries = exp(fx.BULLET_ORG_RESUME)
        self.assertEqual([(e.title, e.organization) for e in entries],
                         [("Senior Backend Engineer", "Acme Payments"), ("Software Engineer", "Globex Data")])
        first, second = entries
        self.assertEqual((first.start, first.end, first.is_current), (date(2021, 1, 1), None, True))
        self.assertEqual((second.start, second.end), (date(2017, 7, 1), date(2020, 12, 1)))
        self.assertTrue(first.default_accept and second.default_accept)
        self.assertEqual({e.layout for e in entries}, {"title_in_header"})

    def test_EX7_title_and_org_on_lines_above_a_date_only_line(self):
        entries = exp(fx.LABEL_NAME_RESUME)
        self.assertEqual([(e.title, e.organization, e.layout) for e in entries],
                         [("Backend Developer", "Initech", "title_in_previous")])

    def test_EX7_org_with_dates_then_the_title_on_the_next_line(self):
        entries = exp(resume("Initech  Mar 2019 - Nov 2022\nBackend Developer\n- Built tools in Python."))
        self.assertEqual([(e.title, e.organization, e.layout) for e in entries],
                         [("Backend Developer", "Initech", "title_in_next")])

    def test_EX7_a_location_line_below_the_anchor_is_not_taken_as_the_employer(self):
        """Found by the LinkedIn-style layout: Org / Title / dates / City. The city below must not beat the employer above."""
        entries = exp(resume("Initech\nBackend Developer\nMar 2019 - Nov 2022\nPune"))
        self.assertEqual([(e.title, e.organization) for e in entries], [("Backend Developer", "Initech")])

    def test_EX7_the_employer_preference_depends_on_where_the_title_was_found(self):
        self.assertEqual(ent._ORG_PREFERENCE["H"][0], "P")  # title in the header: employer is above
        self.assertEqual(ent._ORG_PREFERENCE["P"][0], "P")  # title on its own line: employer next to it
        self.assertEqual(ent._ORG_PREFERENCE["N"][0], "H")  # title below: employer in the header

    def test_EX7_title_at_org_on_one_line(self):
        for line in ("Backend Developer at Initech  Mar 2019 - Nov 2022", "Backend Developer @ Initech  Mar 2019 - Nov 2022",
                     "Backend Developer | Initech  Mar 2019 - Nov 2022", "Backend Developer - Initech  Mar 2019 - Nov 2022"):
            with self.subTest(line=line):
                self.assertEqual([(e.title, e.organization) for e in exp(resume(line))],
                                 [("Backend Developer", "Initech")])

    def test_EX7_a_pipe_or_dash_without_spaces_splits_a_title_from_a_tech_list(self):
        entries = exp(resume("Initech\nBackend Developer| Python, Django, AWS  Mar 2019 - Nov 2022"))
        self.assertEqual([(e.title, e.organization) for e in entries], [("Backend Developer", "Initech")])

    def test_EX8_a_glued_location_remote_or_qualifier_is_removed_from_names(self):
        entries = exp(resume("Initech Remote\nBackend Developer (Contract) - Pune, Maharashtra  Mar 2019 - Nov 2022"))
        self.assertEqual([(e.title, e.organization) for e in entries], [("Backend Developer", "Initech")])

    def test_EX9_two_titles_are_ambiguous_and_default_to_reject_with_the_raw_header(self):
        entries = exp(resume("Staff Engineer\nSenior Developer  Mar 2019 - Nov 2022"))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].status, entries[0].default_accept, entries[0].needs_check), ("ambiguous", False, True))

    def test_EX7_a_title_without_any_employer_is_title_only_and_not_a_default(self):
        entries = exp(resume("Backend Developer  Mar 2019 - Nov 2022"))
        self.assertEqual((entries[0].status, entries[0].organization, entries[0].default_accept), ("title_only", "", False))

    def test_EX7_the_layout_vote_resolves_an_ambiguous_entry_and_flags_it(self):
        body = (
            "Initech\nBackend Developer  Mar 2019 - Nov 2022\n"
            "Hooli\nData Engineer  Jan 2016 - Feb 2019\n"
            "Pied Piper  Jan 2014 - Dec 2015\n"
        )
        entries = exp(resume(body))
        self.assertEqual(len(entries), 3)
        by_org = {e.organization: e for e in entries if e.organization}
        self.assertEqual(by_org["Initech"].title, "Backend Developer")
        self.assertFalse(by_org["Initech"].layout_inferred)
        last = entries[-1]
        self.assertFalse(last.default_accept)  # inferred or unresolved entries are never defaults

    def test_EX10_the_next_entrys_org_line_is_not_part_of_the_previous_block(self):
        body = (
            "Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Built tools in Python.\n"
            "Hooli\nData Engineer  Jan 2016 - Feb 2019\n- Built pipelines in Go and Kafka.\n"
        )
        first, second = exp(resume(body))
        self.assertIn("Python", first.skills)
        self.assertNotIn("Go", first.skills)
        self.assertIn("Go", second.skills)
        self.assertNotIn("Python", second.skills)

    def test_EX13_date_ranges_in_education_never_become_entries(self):
        text = resume("Initech\nBackend Developer  Mar 2019 - Nov 2022") + "Education\nBSc Computer Science 2014 - 2018\n"
        self.assertEqual(len(exp(text)), 1)
        only_edu = "Jane Doe\nEducation\nExample University\nB.Tech Computer Science  2012 - 2016\nM.S. Data Science  2016 - 2018\n" + "x " * 150
        self.assertEqual(run(only_edu), [])

    def test_EX13_a_degree_header_is_never_an_experience_entry_even_under_experience(self):
        self.assertEqual(exp(resume("Example University\nB.Tech Computer Science  2012 - 2016")), [])

    def test_S5_without_an_experience_heading_entries_are_a_flagged_fallback_that_defaults_to_reject(self):
        text = "Jane Doe\nSummary\nBuilder.\nInitech\nBackend Developer  Mar 2019 - Nov 2022\n" + "x " * 150
        entries = exp(text)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].rule_fallback)
        self.assertFalse(entries[0].default_accept)

    def test_EX4_implausible_dates_are_dropped_and_the_entry_needs_a_check(self):
        entries = exp(resume("Initech\nBackend Developer  Mar 2022 - Jan 2019"))
        self.assertEqual((entries[0].start, entries[0].end, entries[0].is_current), (None, None, False))
        self.assertTrue(entries[0].needs_check)
        self.assertFalse(entries[0].default_accept)

    def test_EX16_year_only_dates_default_to_reject(self):
        entries = exp(resume("Initech\nBackend Developer  2019 - 2022"))
        self.assertEqual(entries[0].precision, "year")
        self.assertFalse(entries[0].default_accept)

    def test_EX16_link_text_brackets_and_long_names_default_to_reject(self):
        for header in ("Initech Github Link\nBackend Developer  Mar 2019 - Nov 2022",
                       "Initech [Link] Systems\nBackend Developer  Mar 2019 - Nov 2022",
                       "A Very Long Company Name With Far Too Many Words\nBackend Developer  Mar 2019 - Nov 2022"):
            with self.subTest(header=header):
                self.assertFalse(exp(resume(header))[0].default_accept)

    def test_EX8_a_trailing_bracketed_qualifier_is_peeled_leaving_a_clean_name(self):
        entry = exp(resume("Initech [Link to repo]\nBackend Developer  Mar 2019 - Nov 2022"))[0]
        self.assertEqual(entry.organization, "Initech")
        self.assertTrue(entry.default_accept)

    def test_N8_a_kerning_split_capital_is_repaired_before_extraction(self):
        entries = exp(resume("Hooli T echnologies\nBackend Developer  Mar 2019 - Nov 2022"))
        self.assertEqual(entries[0].organization, "Hooli Technologies")

    def test_EX11_the_same_entry_twice_is_merged_with_united_skills(self):
        body = ("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Python work.\n"
                "Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Go work.\n")
        entries = exp(resume(body))
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0].skills), {"Python", "Go"})

    def test_EX12_entry_counts_are_capped(self):
        body = "\n".join(f"Org{i}\nEngineer  Jan {2000 + i % 25} - Feb {2000 + i % 25}" for i in range(40))
        self.assertLessEqual(len(exp(resume(body))), ent.MAX_EXPERIENCE)


class SkillAttachmentTests(SimpleTestCase):
    def skills(self, body, **kw):
        return list(exp(resume(body, **kw))[0].skills)

    def test_EA1_only_skills_in_the_entrys_own_text_attach(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Built tools with Django on AWS.")
        self.assertEqual(got, ["Django", "AWS"])  # Go and Kubernetes are declared but not mentioned here

    def test_EA1_skills_from_the_summary_or_skills_section_do_not_attach(self):
        self.assertEqual(self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Did several things."), [])

    def test_EA2_boundaries_java_is_not_inside_javascript_and_special_tokens_work(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- JavaScript, C++, C#, .NET and Node.js services.",
                          skills="JavaScript, C++, C#, .NET, Node.js, Java")
        self.assertEqual(got, ["JavaScript", "C++", "C#", ".NET", "Node.js"])
        self.assertNotIn("Java", got)

    def test_EA3_common_english_words_do_not_become_skills(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Led go to market, express interest, spring cleaning.",
                          skills="Python")
        self.assertEqual(got, [])

    def test_EA3_an_ambiguous_word_attaches_only_exact_case_and_when_declared_or_in_a_tech_list(self):
        declared = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Wrote services in Go.", skills="Go, Python")
        self.assertEqual(declared, ["Go"])
        listed = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Stack: Go, Docker, Kafka, Redis.", skills="Python")
        self.assertIn("Go", listed)
        wrong_case = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Wrote services in go.", skills="Go")
        self.assertNotIn("Go", wrong_case)
        undeclared = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Wrote services in Go.", skills="Python")
        self.assertNotIn("Go", undeclared)

    def test_EA4_variants_fold_to_the_canonical_skill(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Python3, React.js, Postgres and K8s.",
                          skills="Python, React, PostgreSQL, Kubernetes")
        self.assertEqual(got, ["Python", "React", "PostgreSQL", "Kubernetes"])

    def test_EA1_lexicon_skills_attach_even_when_the_resume_did_not_declare_them(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Operated Kafka and Terraform.", skills="Python")
        self.assertEqual(got, ["Kafka", "Terraform"])

    def test_EA1_a_declared_skill_outside_the_lexicon_attaches_when_mentioned(self):
        got = self.skills("Initech\nBackend Developer  Mar 2019 - Nov 2022\n- Used ObscureFramework daily.", skills="ObscureFramework")
        self.assertEqual(got, ["ObscureFramework"])

    def test_EX12_skills_per_entry_are_capped(self):
        many = ", ".join(f"Skill{i}x" for i in range(60))
        got = self.skills(f"Initech\nBackend Developer  Mar 2019 - Nov 2022\n- {many}", skills=many)
        self.assertLessEqual(len(got), ent.MAX_ENTRY_SKILLS)


class ProjectTests(SimpleTestCase):
    def projects(self, text):
        return [e for e in run(text) if e.kind == "project"]

    def test_EX15_a_dated_project(self):
        text = "Jane Doe\nProjects\nPayments Dashboard  Jan 2021 - Mar 2021\n- Built with React and Node.js.\nSkills\nReact, Node.js\n"
        projects = self.projects(text)
        self.assertEqual([(p.title, p.organization, p.start) for p in projects], [("Payments Dashboard", "", date(2021, 1, 1))])
        self.assertEqual(list(projects[0].skills), ["React", "Node.js"])

    def test_EX15_an_undated_project_with_a_tech_list_and_bullets(self):
        text = ("Jane Doe\nProjects\nPayments Dashboard | React, Node.js\n- Built the UI.\n- Added tests with Jest.\n"
                "Skills\nReact, Node.js, Jest\n")
        projects = self.projects(text)
        self.assertEqual([p.title for p in projects], ["Payments Dashboard"])
        self.assertEqual(set(projects[0].skills), {"React", "Node.js", "Jest"})
        self.assertTrue(projects[0].default_accept)

    def test_EX15_a_project_with_no_grounded_skill_and_no_dates_is_not_proposed(self):
        text = "Jane Doe\nProjects\nPayments Dashboard\n- Built a thing for the team.\n- Shipped it.\nSkills\nPython\n"
        self.assertEqual(self.projects(text), [])

    def test_EX15_detail_bullets_are_not_projects(self):
        text = "Jane Doe\nProjects\nPayments Dashboard | Python\n- Built the UI.\n- Added tests.\n- Wrote docs.\nSkills\nPython\n"
        self.assertEqual(len(self.projects(text)), 1)

    def test_EX15_project_entries_have_no_organization(self):
        text = "Jane Doe\nProjects\nPayments Dashboard  Jan 2021 - Mar 2021\n- Built with Python.\nSkills\nPython\n"
        self.assertEqual(self.projects(text)[0].organization, "")


class GroundingTests(SimpleTestCase):
    def entries(self):
        normalized = normalize_text(fx.BULLET_ORG_RESUME)
        return normalized, ent.extract_entries(normalized, TODAY)

    def test_G2_every_extracted_entry_is_grounded(self):
        normalized, entries = self.entries()
        self.assertTrue(entries)
        for entry in entries:
            self.assertTrue(ent.entry_grounded(normalized.text, entry))

    def test_G2_forged_values_are_not_grounded(self):
        normalized, entries = self.entries()
        entry = entries[0]
        forged = {
            "title": dataclasses.replace(entry, title="Chief Wizard"),
            "organization": dataclasses.replace(entry, organization="Evil Corp"),
            "start": dataclasses.replace(entry, start=date(2015, 1, 1)),
            "current": dataclasses.replace(entry, is_current=False, end=date(2022, 1, 1)),
            "skill": dataclasses.replace(entry, skills=entry.skills + ("COBOL",), spans={**entry.spans, "skills": entry.spans["skills"] + ((0, 4),)}),
            "skill count": dataclasses.replace(entry, skills=entry.skills + ("COBOL",)),
        }
        for label, bad in forged.items():
            with self.subTest(label):
                self.assertFalse(ent.entry_grounded(normalized.text, bad))

    def test_G2_spans_outside_the_text_or_block_are_not_grounded(self):
        normalized, entries = self.entries()
        entry = entries[0]
        far = (len(normalized.text) + 1, len(normalized.text) + 5)
        self.assertFalse(ent.entry_grounded(normalized.text, dataclasses.replace(entry, spans={**entry.spans, "title": far})))
        self.assertFalse(ent.entry_grounded(normalized.text, dataclasses.replace(entry, spans={**entry.spans, "dates": far})))


class DerivedFieldTests(SimpleTestCase):
    def fields(self, text):
        return {p.field: p.value for p in pipeline.run_rules(normalize_text(text), TODAY).fields}

    def test_FE1_the_current_employer_and_recent_titles(self):
        found = self.fields(fx.BULLET_ORG_RESUME)
        self.assertEqual(found["current_employer"], "Acme Payments")
        self.assertEqual(found["target_titles"], ["Senior Backend Engineer", "Software Engineer"])

    def test_FE1_no_current_role_means_no_current_employer(self):
        found = self.fields(resume("Initech\nBackend Developer  Mar 2019 - Nov 2022"))
        self.assertNotIn("current_employer", found)
        self.assertEqual(found["target_titles"], ["Backend Developer"])

    def test_FE1_two_current_roles_with_the_same_start_propose_nothing(self):
        found = self.fields(resume("Initech\nBackend Developer  Mar 2019 - Present\nHooli\nData Engineer  Mar 2019 - Present"))
        self.assertNotIn("current_employer", found)

    def test_FE1_the_most_recent_current_role_wins(self):
        found = self.fields(resume("Initech\nBackend Developer  Mar 2019 - Present\nHooli\nData Engineer  Jan 2022 - Present"))
        self.assertEqual(found["current_employer"], "Hooli")

    def test_FT1_titles_are_deduplicated_and_limited_to_three(self):
        body = "\n".join(f"Org{i}\nBackend Developer{' ' * (i % 2)}  Jan {2010 + i} - Dec {2010 + i}" for i in range(6))
        self.assertLessEqual(len(self.fields(resume(body)).get("target_titles", [])), 3)

    def test_an_ambiguous_entry_never_feeds_a_profile_field(self):
        found = self.fields(resume("Staff Engineer\nSenior Developer  Mar 2019 - Present"))
        self.assertNotIn("current_employer", found)
        self.assertNotIn("target_titles", found)

    def test_pipeline_grounds_everything_and_adds_each_field_once(self):
        extraction = pipeline.run_rules(normalize_text(fx.BULLET_ORG_RESUME), TODAY)
        names = [p.field for p in extraction.fields]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("full_name", names)
        self.assertEqual(len(extraction.entries), 2)


class StorageRoundTripTests(TestCase):
    def test_accepted_entries_store_and_compute_years(self):
        user = get_user_model().objects.create_user("alice", password="pw")
        extraction = pipeline.run_rules(normalize_text(fx.BULLET_ORG_RESUME), TODAY)
        result = apply_entries(user.profile.pk, [e.as_data() for e in extraction.entries if e.default_accept], today=TODAY)
        self.assertEqual(len(result.created), 2)
        stored = ResumeEntry.objects.get(title="Software Engineer")
        self.assertEqual((stored.organization, stored.start_date, stored.end_date), ("Globex Data", date(2017, 7, 1), date(2020, 12, 1)))
        self.assertEqual(stored.skills, ["Go", "Kafka", "Redis"])


class AdversarialTests(SimpleTestCase):
    def run_fast(self, text):
        started = time.monotonic()
        ent.extract_entries(normalize_text(text), TODAY)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_many_date_ranges_and_long_lines(self):
        self.run_fast("Experience\n" + "\n".join(f"Org{i}\nEngineer  Jan 2010 - Feb 2012 " + "word " * 40 for i in range(300)))

    def test_date_like_noise(self):
        self.run_fast("Experience\n" + "\n".join(["2020 - 2021 " * 25, "1/2020 - 2/2021 " * 15, "Jan - Feb - Mar " * 20] * 60))

    def test_a_forty_thousand_character_line_and_many_short_headings(self):
        self.run_fast("Experience\n" + "a" * 39_000)
        self.run_fast("\n".join(["Experience", "Projects", "Education", "Skills"] * 2000))

    def test_deep_separators_and_commas(self):
        self.run_fast("Experience\n" + "\n".join([" - ".join(["Eng"] * 50) + "  Jan 2020 - Feb 2021", ",".join(["x"] * 60)] * 60))
