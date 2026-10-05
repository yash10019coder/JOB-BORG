"""Section detection, grounding and the profile-field rules
(S1-S10, G1-G3, FN, FP, FU, FL, FK, SK; docs/plans/2026-10-05-002-profile-import-rules.md)."""
import dataclasses
import time

from django.test import SimpleTestCase

from apps.accounts.importing import rules
from apps.accounts.importing import sections as sec
from apps.accounts.importing import skills_lexicon as lex
from apps.accounts.importing.grounding import (
    Proposal,
    derive_location,
    derive_name,
    is_grounded,
    live_tags,
    tag_for_skill,
)
from apps.accounts.importing.validators import ImportValueError
from apps.accounts.tests import import_fixtures as fx


def doc(text):
    return rules.prepare(fx.normalized(text))


def fields(text):
    return {p.field: p.value for p in rules.extract_fields(fx.normalized(text))}


def skills(text):
    return [s.name for s in rules.declared_skills(doc(text))]


class SectionTests(SimpleTestCase):
    def kind(self, line):
        return sec.heading_kind(line)

    def test_S2_known_headings_match_in_any_case_and_ignore_ampersand_and_punctuation(self):
        for line in ("Experience", "EXPERIENCE", "work experience", "Professional Experience:", "Skills & Tools"):
            self.assertIn(self.kind(line), (sec.EXPERIENCE, sec.SKILLS), line)
        self.assertEqual(self.kind("Education"), sec.EDUCATION)
        self.assertEqual(self.kind("Open Source Contributions"), sec.PROJECTS)
        self.assertEqual(self.kind("Programming Skills"), sec.SKILLS)
        self.assertEqual(self.kind("Certificates"), sec.CERTS)
        self.assertEqual(self.kind("Soft Skills"), sec.OTHER)
        self.assertEqual(self.kind("Languages"), sec.OTHER)

    def test_S1_a_lone_title_case_word_that_is_not_a_known_name_is_not_a_heading(self):
        for line in ("Python", "React", "Android", "Kotlin", "Hooli", "Acme Payments"):
            self.assertIsNone(self.kind(line), line)

    def test_S1_shouted_or_colon_terminated_unknown_lines_are_headings(self):
        self.assertEqual(self.kind("FRAMEWORKS"), sec.OTHER)
        self.assertEqual(self.kind("Frameworks:"), sec.OTHER)

    def test_S1_bullets_digits_sentences_and_long_lines_are_not_headings(self):
        for line in ("• Education", "Education 2019", "Experience.", "A very long line that is far too long to be a heading at all", ""):
            self.assertIsNone(self.kind(line), line)

    def test_S3_a_section_runs_until_the_next_heading(self):
        lines = ("Jane", "Experience", "a", "b", "Education", "c")
        sections = sec.find_sections(lines)
        self.assertEqual([(s.kind, s.start, s.end) for s in sections],
                         [(sec.EXPERIENCE, 2, 4), (sec.EDUCATION, 5, 6)])
        self.assertEqual(sec.section_of(sections, 3).kind, sec.EXPERIENCE)
        self.assertIsNone(sec.section_of(sections, 0))

    def test_S8_duplicate_sections_are_concatenated(self):
        lines = ("Experience", "a", "Education", "b", "Experience", "c")
        self.assertEqual(sec.lines_of(sec.find_sections(lines), sec.EXPERIENCE), [1, 5])

    def test_N7_noise_lines_are_not_headings(self):
        lines = ("Experience", "a", "Experience", "b")
        self.assertEqual(len(sec.find_sections(lines, noise=frozenset({2}))), 1)

    def test_a_heading_with_content_on_the_same_line(self):
        self.assertEqual(sec.heading_with_content("Technical Skills: Python, Java"), (sec.SKILLS, "Python, Java"))
        self.assertIsNone(sec.heading_with_content("Acme: Engineer"))


class LexiconTests(SimpleTestCase):
    def test_canonical_names_aliases_and_case(self):
        for raw, expected in (("postgres", "PostgreSQL"), ("K8S", "Kubernetes"), ("golang", "Go"),
                              ("node.js", "Node.js"), ("react.js", "React"), ("C++", "C++"), ("cpp", "C++")):
            self.assertEqual(lex.canonical_skill(raw), expected, raw)

    def test_EA4_a_version_suffix_is_ignored(self):
        self.assertEqual(lex.canonical_skill("Python 3.11"), "Python")
        self.assertEqual(lex.canonical_skill("Java 17"), "Java")
        self.assertIsNone(lex.canonical_skill("Basket Weaving 4"))

    def test_unknown_and_empty_have_no_canonical_name(self):
        self.assertIsNone(lex.canonical_skill("Underwater Basketry"))
        self.assertIsNone(lex.canonical_skill(""))
        self.assertIsNone(lex.canonical_skill(None))

    def test_EA3_common_english_words_are_flagged_ambiguous(self):
        for word in ("Go", "R", "Swift", "Rust", "Spark", "Express", "Spring"):
            self.assertTrue(lex.is_ambiguous(word), word)
        self.assertFalse(lex.is_ambiguous("PostgreSQL"))

    def test_FU3_dotted_tech_names_are_known(self):
        for name in ("node.js", "next.js", ".net", "asp.net", "socket.io"):
            self.assertIn(name, lex.DOTTED_TECH)

    def test_spellings_prefer_the_longest_form(self):
        keys = [spelling for spelling, _ in lex.all_spellings()]
        self.assertLess(keys.index("spring boot"), keys.index("spring"))
        self.assertIn("v", lex.SKILLS_LEXICON_VERSION[:0] + "v")  # version constant exists
        self.assertTrue(lex.SKILLS_LEXICON_VERSION)


class GroundingTests(SimpleTestCase):
    def proposal(self, text):
        return next(p for p in rules.extract_fields(fx.normalized(text)) if p.field == "phone")

    def test_G1_a_real_proposal_is_grounded(self):
        normalized = fx.normalized(fx.BULLET_ORG_RESUME)
        for proposal in rules.extract_fields(normalized):
            self.assertTrue(is_grounded(normalized.text, proposal), proposal.field)

    def test_G1_a_value_that_does_not_match_its_span_is_dropped(self):
        normalized = fx.normalized(fx.BULLET_ORG_RESUME)
        proposal = self.proposal(fx.BULLET_ORG_RESUME)
        forged = dataclasses.replace(proposal, value="+91 1111111111")
        self.assertFalse(is_grounded(normalized.text, forged))

    def test_G1_a_span_that_moved_or_is_out_of_range_is_dropped(self):
        normalized = fx.normalized(fx.BULLET_ORG_RESUME)
        proposal = self.proposal(fx.BULLET_ORG_RESUME)
        for spans in (((0, 4),), ((len(normalized.text), len(normalized.text) + 5),), ((5, 5),), ((-1, 3),), ()):
            with self.subTest(spans=spans):
                self.assertFalse(is_grounded(normalized.text, dataclasses.replace(proposal, spans=spans)))

    def test_G1_an_unknown_field_is_never_grounded(self):
        self.assertFalse(is_grounded("abc", Proposal("email", "a@b.co", ((0, 3),), "abc")))

    def test_G3_snippets_are_short_single_line_excerpts(self):
        for proposal in rules.extract_fields(fx.normalized(fx.BULLET_ORG_RESUME)):
            self.assertLessEqual(len(proposal.snippet), 80)
            self.assertNotIn("\n", proposal.snippet)

    def test_FN4_derive_name_rules(self):
        self.assertEqual(derive_name("JOHN SMITH"), "John Smith")
        self.assertEqual(derive_name("José García"), "José García")
        self.assertEqual(derive_name("Jane Doe Email"), "Jane Doe")
        self.assertEqual(derive_name("♂ Jane Doe"), "Jane Doe")
        for bad in ("jane doe", "Jane", "Resume", "Curriculum Vitae", "Software Engineer", "Jane Doe 42", "A B C D E"):
            with self.subTest(bad=bad), self.assertRaises(ImportValueError):
                derive_name(bad)

    def test_FL_derive_location_needs_a_city_and_never_a_skill_or_title(self):
        self.assertEqual(derive_location("Lucknow, India"), ("Lucknow", "IND"))
        self.assertEqual(derive_location("San Francisco, CA"), ("San Francisco", "USA"))
        for bad in ("Remote", "Python, Java, SQL", "Software Engineer, Backend", "Acme Corp, Inc", "Berlin"):
            with self.subTest(bad=bad), self.assertRaises(ImportValueError):
                derive_location(bad)

    def test_FK2_tags_come_from_the_live_vocabulary_and_never_role_tags(self):
        for skill, tag in (("Python", "python"), ("py", "python"), ("golang", "golang"), ("Go", "golang"),
                           ("k8s", "kubernetes"), ("JS", "javascript")):
            self.assertEqual(tag_for_skill(skill), tag, skill)
        for skill in ("Java", "Django", "backend", "devops", "senior", "AWS", ""):
            self.assertIsNone(tag_for_skill(skill), skill)
        self.assertIn("python", live_tags())


class NameTests(SimpleTestCase):
    def name(self, text):
        return fields(text).get("full_name")

    def test_FN1_a_plain_first_line(self):
        self.assertEqual(self.name(fx.BULLET_ORG_RESUME), "Jane Doe")

    def test_FN2_a_name_followed_by_an_email_label_is_cut_at_the_label(self):
        self.assertEqual(self.name(fx.LABEL_NAME_RESUME), "John Smith")

    def test_FN4_all_caps_is_title_cased(self):
        self.assertEqual(self.name(fx.CAPS_RESUME), "Priya Raman")

    def test_FN2_a_degree_suffix_after_a_comma_is_dropped_but_an_address_is_not_a_name(self):
        self.assertEqual(self.name("Jane Doe, PhD\njane@example.com\n" + "x " * 150), "Jane Doe")
        self.assertIsNone(self.name("New Delhi, India\njane@example.com\n" + "x " * 150))

    def test_FN3_a_job_title_or_heading_word_is_not_a_name(self):
        self.assertIsNone(self.name(fx.NO_HEADER_RESUME))
        self.assertIsNone(self.name("RESUME\nCurriculum Vitae\n" + "x " * 150))

    def test_FN1_only_the_first_three_lines_are_considered_and_the_contact_block_stops_the_search(self):
        text = "jane@example.com | 12345678\nSecond Line\n" + "x " * 150
        self.assertIsNone(self.name(text))
        far = "\n".join(["Curriculum Vitae", "RESUME", "Page 1 of 2", "Jane Doe"]) + "\n" + "x " * 150
        self.assertIsNone(self.name(far))

    def test_FN5_the_first_accepted_line_wins(self):
        self.assertEqual(self.name("Jane Doe\nJohn Smith\n" + "x " * 150), "Jane Doe")

    def test_N7_a_repeated_page_header_is_not_a_second_candidate_but_the_first_line_still_counts(self):
        self.assertEqual(self.name(fx.REPEATED_HEADER_RESUME), "Alex Kim")


class PhoneTests(SimpleTestCase):
    def phone(self, text):
        return fields(text).get("phone")

    def test_FP2_the_number_is_kept_as_written(self):
        self.assertEqual(self.phone(fx.BULLET_ORG_RESUME), "+91 9876543210")
        self.assertEqual(self.phone(fx.REPEATED_HEADER_RESUME), "020 7946 0958")

    def test_FP4_an_icon_font_label_glued_to_the_number_is_accepted(self):
        self.assertEqual(self.phone(fx.LABEL_NAME_RESUME), "+1 415 555 0134")
        self.assertEqual(self.phone("Jane Doe\n♂¶obile+91 9876543210 | x@y.co\n" + "x " * 150), "+91 9876543210")

    def test_FP4_a_truncated_icon_label_glued_to_an_international_number_is_accepted(self):
        for glue in ("♂ne", "/ne", "ne", "♂¶obile", "tel"):
            with self.subTest(glue=glue):
                self.assertEqual(self.phone(f"Jane Doe\n{glue}+91 9876543210 | x@y.co\n" + "x " * 150), "+91 9876543210")

    def test_FP3_only_an_international_number_may_be_glued_to_letters(self):
        for glue in ("ne", "name", "jane"):
            with self.subTest(glue=glue):
                self.assertIsNone(self.phone(f"Jane Doe\n{glue}9876543210 | x@y.co\n" + "x " * 150))

    def test_FP2_a_plus_inside_parentheses_is_accepted(self):
        self.assertEqual(self.phone(fx.CAPS_RESUME), "(+91) 98765 43210")

    def test_FP3_digits_inside_urls_emails_and_handles_are_not_phones(self):
        for line in ("linkedin.com/in/jane-doe-1234567890", "jane1234567890@example.com", "github.com/jane1234567890",
                     "handle jane1234567890x"):
            with self.subTest(line=line):
                self.assertIsNone(self.phone(f"Jane Doe\n{line}\n" + "x " * 150))

    def test_FP3_date_ranges_and_decimals_are_not_phones(self):
        for line in ("2019 - 2023", "Jan 2020 - 2021 ... 2019 – 2023", "CGPA 8.75 / 10", "ID 12345"):
            with self.subTest(line=line):
                self.assertIsNone(self.phone(f"Jane Doe\n{line}\n" + "x " * 150))

    def test_FP4_a_labelled_number_beats_an_earlier_unlabelled_one(self):
        text = "Jane Doe\nRef 99999999 | Phone: 12345678\n" + "x " * 150
        self.assertEqual(self.phone(text), "12345678")

    def test_FP1_only_the_contact_zone_counts(self):
        filler = "\n".join(f"line {i}" for i in range(20))
        self.assertIsNone(self.phone("Jane Doe\n" + filler + "\nPhone: 12345678\n" + "x " * 150))

    def test_FP2_no_country_code_is_ever_added(self):
        self.assertEqual(self.phone("Jane Doe\n9876543210\n" + "x " * 150), "9876543210")


class LinkTests(SimpleTestCase):
    def test_FU1_scheme_less_linkedin_is_canonicalised(self):
        self.assertEqual(fields(fx.BULLET_ORG_RESUME)["linkedin_url"], "https://www.linkedin.com/in/jane-doe-12345")
        self.assertEqual(fields(fx.CAPS_RESUME)["linkedin_url"], "https://www.linkedin.com/in/priya-raman-77")

    def test_FU1_company_pages_are_not_a_profile(self):
        self.assertNotIn("linkedin_url", fields("Jane Doe\nlinkedin.com/company/acme\n" + "x " * 150))

    def test_FU2_a_github_profile_is_taken_but_a_repository_is_not(self):
        self.assertEqual(fields(fx.BULLET_ORG_RESUME)["github_url"], "https://github.com/janedoe")
        self.assertNotIn("github_url", fields("Jane Doe\ngithub.com/janedoe/my-repo\n" + "x " * 150))
        self.assertNotIn("github_url", fields("Jane Doe\ngithub.com/settings\n" + "x " * 150))

    def test_FU3_a_bare_domain_in_the_contact_zone_is_the_portfolio(self):
        self.assertEqual(fields(fx.BULLET_ORG_RESUME)["portfolio_url"], "https://janedoe.dev")

    def test_FU3_a_labelled_website_line_counts_outside_the_zone(self):
        self.assertEqual(fields(fx.CAPS_RESUME)["portfolio_url"], "https://priyaraman.dev")

    def test_FU3_dotted_tech_names_and_file_names_are_not_domains(self):
        text = "Jane Doe\nNode.js | Next.js | resume.pdf | ASP.NET | Socket.io\n" + "x " * 150
        self.assertNotIn("portfolio_url", fields(text))

    def test_FU3_an_email_domain_is_never_a_portfolio(self):
        self.assertNotIn("portfolio_url", fields("Jane Doe\njane@janedoe.dev\n" + "x " * 150))

    def test_FU3_linkedin_and_github_are_not_the_portfolio(self):
        text = "Jane Doe\nlinkedin.com/in/jane-doe-12345 | github.com/janedoe\n" + "x " * 150
        self.assertNotIn("portfolio_url", fields(text))

    def test_FU3_a_bare_domain_outside_the_zone_is_ignored_unless_labelled(self):
        filler = "\n".join(f"line {i}" for i in range(15))
        self.assertNotIn("portfolio_url", fields("Jane Doe\n" + filler + "\njanedoe.dev\n" + "x " * 150))

    def test_FU4_http_and_dangerous_schemes_are_never_proposed(self):
        for token in ("http://janedoe.dev", "javascript:alert(1)", "ftp://janedoe.dev"):
            with self.subTest(token=token):
                self.assertNotIn("portfolio_url", fields(f"Jane Doe\n{token}\n" + "x " * 150))


class LocationTests(SimpleTestCase):
    def test_FL1_a_city_region_country_token_in_the_header(self):
        found = fields(fx.CAPS_RESUME)
        self.assertEqual((found["location_city"], found["location_country"]), ("Bengaluru", "IND"))

    def test_FL3_an_employer_line_is_never_a_location(self):
        found = fields(fx.BULLET_ORG_RESUME)
        self.assertNotIn("location_city", found)  # "Pune, Maharashtra" appears only on an employer line

    def test_FL3_remote_skill_lists_and_phone_codes_are_not_locations(self):
        for line in ("Remote", "Python, Java, SQL", "Open to relocate"):
            with self.subTest(line=line):
                self.assertNotIn("location_city", fields(f"Jane Doe\n{line} | +91 9876543210\n" + "x " * 150))

    def test_FL2_a_token_that_does_not_resolve_to_a_city_is_dropped(self):
        self.assertNotIn("location_city", fields("Jane Doe\nAcme Widgets, Inc | 12345678\n" + "x " * 150))


class DeclaredSkillTests(SimpleTestCase):
    def test_SK2_glued_labels_are_split_and_discarded(self):
        found = skills(fx.BULLET_ORG_RESUME)
        for expected in ("Python", "Go", "SQL", "AWS", "Docker", "Kubernetes", "Terraform", "PostgreSQL", "Redis", "Git", "Linux", "Kafka"):
            self.assertIn(expected, found)
        for label in ("Languages", "Cloud & Infra", "Databases", "Tools", "Cloud"):
            self.assertNotIn(label, found)

    def test_SK2_a_label_with_no_space_after_the_colon(self):
        text = "Technical Skills\nLanguages:Python, Java Tools:Git\n"
        self.assertEqual(skills(text), ["Python", "Java", "Git"])

    def test_SK3_slash_does_not_split_ci_cd_and_special_tokens_survive(self):
        found = skills("Skills\nCI/CD, C++, C#, .NET, Node.js, TCP/IP\n")
        for expected in ("CI/CD", "C++", "C#", ".NET", "Node.js", "TCP/IP"):
            self.assertIn(expected, found)

    def test_SK4_group_parentheses_expand_and_drop_the_group(self):
        found = skills("Skills\nCloud (AWS, GCP), Testing (Jest, pytest), Databases (PostgreSQL)\n")
        self.assertEqual(found, ["AWS", "GCP", "Jest", "pytest", "PostgreSQL"])

    def test_SK4_a_group_that_is_itself_a_skill_is_kept(self):
        # the group is kept (it is a skill) and its parts are expanded; a lone
        # parenthetical on a known skill adds nothing
        self.assertEqual(skills("Skills\nDocker (Compose, Swarm), React (Hooks)\n"), ["Docker", "Compose", "Swarm", "React"])

    def test_SK5_proficiency_and_years_are_stripped_and_noise_items_dropped(self):
        found = skills("Skills\nPython (Advanced), Java (3 years), Go - Expert, and, etc, Knowledge\n")
        self.assertEqual(found, ["Python", "Java", "Go"])

    def test_SK5_sentences_and_long_items_are_not_skills(self):
        found = skills("Skills\nI have built many systems over many years., " + "word " * 12 + ", Python\n")
        self.assertEqual(found, ["Python"])

    def test_SK6_duplicates_collapse_case_insensitively(self):
        self.assertEqual(skills("Skills\npython, Python, PYTHON, py\n"), ["Python"])

    def test_SK1_spoken_languages_and_soft_skills_are_never_skills(self):
        text = "Technical Skills\nPython\nLanguages\nEnglish, Hindi\nSoft Skills\nLeadership, Teamwork\n"
        self.assertEqual(skills(text), ["Python"])

    def test_SK1_an_inline_skills_heading_line_is_read(self):
        self.assertEqual(skills(fx.CAPS_RESUME), ["Python", "JavaScript", "React", "Node.js", "Kubernetes"])

    def test_SK7_unknown_skills_declared_by_the_resume_are_kept_in_its_spelling(self):
        self.assertEqual(skills("Skills\nObscureFramework, Python\n"), ["ObscureFramework", "Python"])

    def test_one_skill_per_line_layout(self):
        self.assertEqual(skills("Programming Skills\nPython\nGo\nReact\n"), ["Python", "Go", "React"])

    def test_the_declared_skill_cap(self):
        many = ", ".join(f"Skill{i}x" for i in range(200))
        self.assertLessEqual(len(skills(f"Skills\n{many}\n")), rules.MAX_DECLARED_SKILLS)


class TagTests(SimpleTestCase):
    def test_FK1_only_live_non_role_tags_are_proposed_in_order(self):
        self.assertEqual(fields(fx.BULLET_ORG_RESUME)["target_tags"], ["python", "golang", "kubernetes"])

    def test_FK1_aliases_map_to_the_same_tag_once(self):
        self.assertEqual(fields("Skills\npy, Python, python3, golang, Go\n" + "x " * 150)["target_tags"], ["python", "golang"])

    def test_FK2_skills_with_no_tag_propose_nothing(self):
        self.assertNotIn("target_tags", fields("Skills\nJava, Django, AWS\n" + "x " * 150))

    def test_FK2_role_words_in_a_skills_list_are_never_tags(self):
        self.assertNotIn("target_tags", fields("Skills\nbackend, devops, senior, remote\n" + "x " * 150))


class LinkedInLayoutTests(SimpleTestCase):
    EXPORT = """\
Contact
www.linkedin.com/in/jane-doe-12345 (LinkedIn)
Top Skills
Python
Go
Languages
English (Native)
Jane Doe
Staff Software Engineer at Acme
Pune, Maharashtra, India
Summary
Builds reliable services.
Experience
Acme
Staff Software Engineer
January 2020 - Present (6 years 9 months)
Pune
"""

    def test_S9_both_contact_and_top_skills_headings_are_required(self):
        self.assertTrue(rules.is_linkedin_export(doc(self.EXPORT)))
        self.assertFalse(rules.is_linkedin_export(doc(fx.BULLET_ORG_RESUME)))
        no_top = self.EXPORT.replace("Top Skills\n", "")
        self.assertFalse(rules.is_linkedin_export(doc(no_top)))

    def test_S9_an_ordinary_resume_with_common_headings_is_not_an_export(self):
        for text in (fx.CAPS_RESUME, fx.LABEL_NAME_RESUME, fx.REPEATED_HEADER_RESUME):
            self.assertFalse(rules.is_linkedin_export(doc(text)))

    def test_S10_header_fields_are_marked_provisional(self):
        proposals = {p.field: p for p in rules.extract_fields(fx.normalized(self.EXPORT + "x " * 200))}
        self.assertEqual(proposals["full_name"].value, "Jane Doe")
        self.assertEqual(proposals["headline"].value, "Staff Software Engineer at Acme")
        self.assertEqual(proposals["location_city"].value, "Pune")
        for field in ("full_name", "headline", "location_city"):
            self.assertTrue(proposals[field].provisional, field)


class PipelineTests(SimpleTestCase):
    def test_a_full_resume_yields_the_expected_fields(self):
        found = fields(fx.BULLET_ORG_RESUME)
        self.assertEqual(
            {k: found[k] for k in ("full_name", "phone", "linkedin_url", "github_url", "portfolio_url")},
            {"full_name": "Jane Doe", "phone": "+91 9876543210",
             "linkedin_url": "https://www.linkedin.com/in/jane-doe-12345",
             "github_url": "https://github.com/janedoe", "portfolio_url": "https://janedoe.dev"},
        )

    def test_every_field_has_at_most_one_proposal(self):
        names = [p.field for p in rules.extract_fields(fx.normalized(fx.BULLET_ORG_RESUME))]
        self.assertEqual(len(names), len(set(names)))

    def test_no_proposal_is_ever_made_for_a_non_importable_field(self):
        from apps.accounts.services.profile_fields import IMPORTABLE_FIELDS

        for text in (fx.BULLET_ORG_RESUME, fx.LABEL_NAME_RESUME, fx.CAPS_RESUME):
            for proposal in rules.extract_fields(fx.normalized(text)):
                self.assertIn(proposal.field, IMPORTABLE_FIELDS)

    def test_Z1_email_address_date_of_birth_and_the_like_yield_nothing(self):
        text = ("Jane Doe\nEmail: jane@example.com\nDate of Birth: 12 March 1990\nGender: Female\n"
                "Nationality: Indian\nMarital Status: Single\nSalary expectation: 20 LPA\n" + "x " * 150)
        self.assertEqual(set(fields(text)) - {"full_name"}, set())

    def test_a_document_with_nothing_recognisable_yields_nothing(self):
        self.assertEqual(fields("x " * 200), {})

    def test_the_input_type_is_checked(self):
        with self.assertRaises(TypeError):
            rules.extract_fields("plain text")

    def test_N7_probes_report_booleans_for_import_eval(self):
        normalized = fx.normalized(fx.BULLET_ORG_RESUME)
        self.assertTrue(rules.PROBES["full_name"](normalized))
        self.assertTrue(rules.PROBES["declared_skills"](normalized))
        self.assertFalse(rules.PROBES["linkedin_export"](normalized))
        self.assertFalse(rules.PROBES["headline"](normalized))


class AdversarialTests(SimpleTestCase):
    """Bounded input: pathological text must finish quickly (FR9.3, N5/N6)."""

    def run_fast(self, text):
        started = time.monotonic()
        rules.extract_fields(fx.normalized(text))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_long_runs_of_digits_and_separators(self):
        self.run_fast("\n".join(["1 " * 140, "(((((" * 50, "-" * 280, "9" * 280] * 40))

    def test_long_runs_of_dots_colons_and_pipes(self):
        self.run_fast("\n".join([":" * 290, "|" * 290, ("a." * 140), ("Label: " * 40)] * 40))

    def test_a_forty_thousand_character_single_line_and_many_short_lines(self):
        self.run_fast("a" * 40_000)
        self.run_fast("\n".join("Skills: " + "x, " * 90 for _ in range(150)))
        self.run_fast("\n".join(["A B C D E F G H"] * 3000))

    def test_deeply_nested_parentheses_in_skills(self):
        self.run_fast("Skills\n" + "(" * 500 + "Python" + ")" * 500 + "\n" + ("Cloud (" * 100) + "\n")
