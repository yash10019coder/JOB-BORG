"""Stored entries and years per skill (rules V3, EX4, EX5, EX8, EX11, EX12, R7, R8, Y1-Y4)."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import ResumeEntry
from apps.accounts.services.resume_facts import (
    EntryValueError,
    apply_entries,
    clean_entry,
    clean_skills,
    dates_plausible,
    delete_entry,
    format_years,
    natural_key,
    parse_month,
    years_by_skill,
)

User = get_user_model()
TODAY = date(2026, 10, 5)


def entry(**extra):
    data = {
        "kind": "experience", "title": "Backend Engineer", "organization": "Acme",
        "start": "2022-01", "end": "2024-06", "is_current": False, "skills": ["Python", "Django"],
    }
    data.update(extra)
    return data


def error_code(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except EntryValueError as exc:
        return exc.code
    return None


class DatePlausibilityTests(SimpleTestCase):
    def test_EX4_valid_ranges(self):
        self.assertTrue(dates_plausible(date(2020, 1, 1), date(2022, 6, 1), False, TODAY))
        self.assertTrue(dates_plausible(date(2020, 1, 1), None, True, TODAY))
        self.assertTrue(dates_plausible(None, None, False, TODAY))

    def test_EX4_implausible_ranges_are_rejected(self):
        cases = {
            "before 1970": (date(1965, 1, 1), date(1966, 1, 1), False),
            "end before start": (date(2022, 1, 1), date(2021, 1, 1), False),
            "future end": (date(2024, 1, 1), date(2027, 1, 1), False),
            "future start": (date(2027, 1, 1), None, True),
            "current with an end": (date(2020, 1, 1), date(2022, 1, 1), True),
            "over 50 years": (date(1970, 1, 1), date(2025, 1, 1), False),
        }
        for label, (start, end, current) in cases.items():
            with self.subTest(label):
                self.assertFalse(dates_plausible(start, end, current, TODAY))

    def test_parse_month_accepts_strings_and_dates_only_in_month_form(self):
        self.assertEqual(parse_month("2022-03"), date(2022, 3, 1))
        self.assertEqual(parse_month(date(2022, 3, 17)), date(2022, 3, 1))
        self.assertIsNone(parse_month(None))
        for bad in ("2022-13", "March 2022", "2022-3", "22-03"):
            with self.subTest(bad=bad):
                self.assertEqual(error_code(parse_month, bad), "bad_date")


class CleanEntryTests(SimpleTestCase):
    def test_a_valid_entry_is_cleaned(self):
        cleaned = clean_entry(entry(title="  Backend  Engineer "), TODAY)
        self.assertEqual(cleaned["title"], "Backend Engineer")
        self.assertEqual(cleaned["start_date"], date(2022, 1, 1))
        self.assertEqual(cleaned["end_date"], date(2024, 6, 1))

    def test_EX8_title_and_organization_rules(self):
        self.assertEqual(error_code(clean_entry, entry(title="X"), TODAY), "bad_text_length")
        self.assertEqual(error_code(clean_entry, entry(title="Built many things."), TODAY), "sentence_title")
        self.assertEqual(error_code(clean_entry, entry(title="<b>Eng</b>"), TODAY), "bad_text_markup")
        self.assertEqual(error_code(clean_entry, entry(organization="A" * 101), TODAY), "bad_text_length")
        self.assertEqual(clean_entry(entry(organization=""), TODAY)["organization"], "")

    def test_EX3_a_current_role_has_no_end_date(self):
        cleaned = clean_entry(entry(is_current=True, end="2024-06"), TODAY)
        self.assertIsNone(cleaned["end_date"])

    def test_EX4_implausible_dates_are_rejected(self):
        self.assertEqual(error_code(clean_entry, entry(start="2024-01", end="2023-01"), TODAY), "bad_dates")
        self.assertEqual(error_code(clean_entry, entry(end="2030-01"), TODAY), "bad_dates")

    def test_kind_and_precision_are_checked(self):
        self.assertEqual(error_code(clean_entry, entry(kind="education"), TODAY), "bad_kind")
        self.assertEqual(error_code(clean_entry, entry(precision="day"), TODAY), "bad_precision")

    def test_EX12_skills_are_deduplicated_normalized_and_capped(self):
        self.assertEqual(clean_skills(["Python", "python", " C++ ", ".NET"]), ["Python", "C++", ".NET"])
        self.assertEqual(error_code(clean_skills, [f"s{i}x" for i in range(31)]), "too_many_skills")
        self.assertEqual(error_code(clean_skills, ["x" * 41]), "bad_skill")
        self.assertEqual(error_code(clean_skills, "python"), "bad_skills")
        self.assertEqual(clean_skills(None), [])

    def test_EX11_the_natural_key_ignores_case_and_spacing_and_uses_the_start_month(self):
        a = natural_key("experience", "Acme  Corp", "Backend Engineer", date(2022, 1, 1))
        b = natural_key("experience", "acme corp", "backend  engineer", date(2022, 1, 20))
        self.assertEqual(a, b)
        self.assertNotEqual(a, natural_key("experience", "Acme Corp", "Backend Engineer", date(2022, 2, 1)))
        self.assertIn("nodate", natural_key("project", "", "Site", None))


class ApplyEntriesTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile

    def apply(self, *entries, profile=None):
        return apply_entries((profile or self.profile).pk, list(entries), today=TODAY)

    def test_R7_new_entries_are_created_as_imported(self):
        result = self.apply(entry(), entry(kind="project", title="Side Site", organization="", start=None, end=None, skills=["React"]))
        self.assertEqual(len(result.created), 2)
        row = ResumeEntry.objects.get(kind="experience")
        self.assertEqual((row.source, row.skills, row.precision), ("imported", ["Python", "Django"], "month"))

    def test_R7_an_existing_imported_entry_is_replaced_in_place(self):
        self.apply(entry())
        result = self.apply(entry(skills=["Python", "Django", "Postgres"], end="2024-09"))
        self.assertEqual((len(result.created), len(result.updated)), (0, 1))
        row = ResumeEntry.objects.get()
        self.assertEqual((row.skills, row.end_date), (["Python", "Django", "Postgres"], date(2024, 9, 1)))
        self.assertEqual(ResumeEntry.objects.count(), 1)

    def test_R7_an_identical_reimport_changes_nothing(self):
        self.apply(entry())
        updated_at = ResumeEntry.objects.get().updated_at
        result = self.apply(entry())
        self.assertEqual((result.unchanged, result.updated), ([result.unchanged[0]], []))
        self.assertEqual(ResumeEntry.objects.get().updated_at, updated_at)

    def test_R7_an_entry_the_user_edited_is_stored_as_user_and_later_kept(self):
        self.apply(entry(title="Senior Backend Engineer", edited=True))
        self.assertEqual(ResumeEntry.objects.get().source, "user")
        result = self.apply(entry(title="Senior Backend Engineer", skills=["Go"]))
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(ResumeEntry.objects.get().skills, ["Python", "Django"])

    def test_R7_reimporting_never_deletes_entries_that_are_absent_from_the_new_import(self):
        self.apply(entry(), entry(title="Data Engineer", start="2019-01", end="2021-12"))
        self.apply(entry())
        self.assertEqual(ResumeEntry.objects.count(), 2)

    def test_the_same_entry_for_another_profile_is_independent(self):
        self.apply(entry())
        self.apply(entry(), profile=self.other)
        self.assertEqual(ResumeEntry.objects.count(), 2)

    def test_V3_one_invalid_entry_writes_nothing_and_says_which(self):
        with self.assertRaises(EntryValueError) as caught:
            self.apply(entry(), entry(title="Bad", start="2025-01", end="2020-01"))
        self.assertEqual(caught.exception.code, "1:bad_dates")
        self.assertEqual(ResumeEntry.objects.count(), 0)

    def test_R8_delete_is_scoped_to_the_owner(self):
        self.apply(entry())
        row = ResumeEntry.objects.get()
        self.assertFalse(delete_entry(self.other, row.pk))
        self.assertTrue(ResumeEntry.objects.filter(pk=row.pk).exists())
        self.assertTrue(delete_entry(self.profile, row.pk))
        self.assertFalse(ResumeEntry.objects.exists())


class YearsPerSkillTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def add(self, start, end=None, skills=("Python",), current=False, kind="experience", precision="month", title=None):
        title = title or f"Role {ResumeEntry.objects.count()}"
        return apply_entries(
            self.profile.pk,
            [{"kind": kind, "title": title, "organization": "Org", "start": start, "end": end,
              "is_current": current, "skills": list(skills), "precision": precision}],
            today=TODAY,
        )

    def years(self):
        return {item.name: item for item in years_by_skill(self.profile, today=TODAY)}

    def test_Y2_months_are_inclusive(self):
        self.add("2022-01", "2022-12")
        self.assertEqual(self.years()["Python"].months, 12)
        self.add("2024-03", "2024-03", skills=["Go"])
        self.assertEqual(self.years()["Go"].months, 1)

    def test_Y1_overlapping_roles_are_not_double_counted(self):
        self.add("2020-01", "2021-12")  # 24 months
        self.add("2021-07", "2022-12")  # overlaps 6 months, ends later
        self.assertEqual(self.years()["Python"].months, 36)  # 2020-01..2022-12

    def test_Y1_gaps_are_not_counted(self):
        self.add("2018-01", "2018-12")
        self.add("2020-01", "2020-12")
        self.assertEqual(self.years()["Python"].months, 24)

    def test_Y1_adjacent_roles_add_up_without_double_counting_a_month(self):
        self.add("2020-01", "2020-06")
        self.add("2020-07", "2020-12")
        self.assertEqual(self.years()["Python"].months, 12)

    def test_Y2_a_current_role_runs_to_today(self):
        self.add("2025-10", None, current=True)
        self.assertEqual(self.years()["Python"].months, 13)  # 2025-10 .. 2026-10 inclusive

    def test_Y2_entries_without_a_start_or_an_end_contribute_nothing(self):
        self.add(None, None, skills=["Rust"])
        self.add("2022-01", None, skills=["Elixir"])  # a single date: undated for years
        self.assertNotIn("Rust", self.years())
        self.assertNotIn("Elixir", self.years())

    def test_Y1_projects_are_excluded(self):
        self.add("2022-01", "2022-12", kind="project", skills=["Svelte"])
        self.assertNotIn("Svelte", self.years())

    def test_Y3_skill_names_match_case_insensitively_and_show_the_most_recent_roles_spelling(self):
        self.add("2020-01", "2020-12", skills=["Python"])
        self.add("2022-01", "2022-12", skills=["python"])
        years = self.years()
        self.assertEqual(list(years), ["python"])  # the 2022 role is the most recent
        self.assertEqual(years["python"].months, 24)

    def test_Y2_year_only_precision_is_flagged_approximate(self):
        self.add("2020-01", "2020-12", precision="year")
        self.assertTrue(self.years()["Python"].approximate)
        self.add("2022-01", "2022-12", skills=["Go"])
        self.assertFalse(self.years()["Go"].approximate)

    def test_results_are_sorted_most_experience_first(self):
        self.add("2020-01", "2020-06", skills=["Go"])
        self.add("2018-01", "2022-12", skills=["Python"])
        self.assertEqual([item.name for item in years_by_skill(self.profile, today=TODAY)], ["Python", "Go"])

    def test_other_profiles_entries_are_not_counted(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        apply_entries(other.pk, [entry(skills=["Python"])], today=TODAY)
        self.assertEqual(years_by_skill(self.profile, today=TODAY), [])

    def test_Y3_display_rounds_to_half_years_and_shows_under_a_year_for_tiny_spans(self):
        cases = {1: "< 1 yr", 2: "< 1 yr", 3: "0.5 yrs", 8: "0.5 yrs", 9: "1 yr", 12: "1 yr",
                 17: "1.5 yrs", 36: "3 yrs", 54: "4.5 yrs"}
        for months, text in cases.items():
            with self.subTest(months=months):
                self.assertEqual(format_years(months), text)
