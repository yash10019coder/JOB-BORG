"""The profile-level skill list and project description/url (rules GHX6, GHX7, GHX8, GHX9)."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import ProfileSkill, ResumeEntry
from apps.accounts.services import profile_skills as ps
from apps.accounts.services.resume_facts import (
    EntryValueError,
    apply_entries,
    clean_description,
    clean_entry,
    years_by_skill,
)

User = get_user_model()
TODAY = date(2026, 10, 5)


def evidence(**extra):
    data = {
        "repo_count": 14, "scope": "recent", "scope_size": 90, "first_seen": "2019-03",
        "last_seen": "2026-09", "repos": ["api", "site"], "sources": ["language"],
    }
    data.update(extra)
    return data


def skill(name="Python", **extra):
    return {"name": name, "origin": "github", "evidence": evidence(**extra)}


def project(**extra):
    data = {
        "kind": "project", "title": "Side Site", "organization": "", "start": "2023-01", "end": "2024-06",
        "skills": ["React"], "description": "A small site.", "url": "https://github.com/alice/side-site",
    }
    data.update(extra)
    return data


class EvidenceSchemaTests(SimpleTestCase):
    def test_GHX6_valid_evidence_is_rebuilt_from_the_fixed_schema(self):
        cleaned = ps.clean_evidence({**evidence(), "extra": "dropped", "repos": ["api", "bad name!", "../x", "ok.repo"]})
        self.assertEqual(cleaned["repos"], ["api", "ok.repo"])
        self.assertNotIn("extra", cleaned)

    def test_GHX6_bad_evidence_is_rejected(self):
        for bad in (
            None, "x", evidence(repo_count=0), evidence(repo_count=100), evidence(scope="all"),
            evidence(first_seen="2019-13"), evidence(first_seen="2027-01", last_seen="2026-01"),
            evidence(repo_count=True), evidence(repo_count="14"),
        ):
            with self.assertRaises(ps.SkillValueError, msg=str(bad)):
                ps.clean_evidence(bad)

    def test_GHX6_repo_list_is_capped_and_sources_are_a_known_set(self):
        cleaned = ps.clean_evidence(evidence(repos=[f"r{i}" for i in range(9)], sources=["topic", "language", "evil"]))
        self.assertEqual(len(cleaned["repos"]), 5)
        self.assertEqual(cleaned["sources"], ["language", "topic"])

    def test_GHX6_skill_names_are_plain_text(self):
        with self.assertRaises(ps.SkillValueError):
            ps.clean_skill(skill("<script>"))
        with self.assertRaises(ps.SkillValueError):
            ps.clean_skill(skill("x" * 41))
        self.assertEqual(ps.clean_skill(skill("C++"))["key"], "c++")


class ApplySkillsTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile

    def test_GHX8_new_skills_are_created(self):
        result = ps.apply_skills(self.profile.pk, [skill("Python"), skill("Django", repo_count=3)])
        self.assertEqual(sorted(result.created), ["django", "python"])
        self.assertEqual(ProfileSkill.objects.get(key="django").evidence["repo_count"], 3)

    def test_GHX8_a_full_run_replaces_evidence_and_an_identical_run_changes_nothing(self):
        ps.apply_skills(self.profile.pk, [skill()])
        again = ps.apply_skills(self.profile.pk, [skill()])
        self.assertEqual((again.created, again.updated, again.unchanged), ([], [], ["python"]))
        newer = ps.apply_skills(self.profile.pk, [skill(repo_count=20, last_seen="2026-10")])
        self.assertEqual(newer.updated, ["python"])
        self.assertEqual(ProfileSkill.objects.get().evidence["repo_count"], 20)

    def test_GHX8_a_light_or_partial_run_never_lowers_richer_evidence(self):
        ps.apply_skills(self.profile.pk, [skill(repo_count=14, first_seen="2019-03", last_seen="2026-09", repos=["a"])])
        weaker = skill(repo_count=4, scope_size=30, first_seen="2022-01", last_seen="2026-10", repos=["b"])
        ps.apply_skills(self.profile.pk, [weaker], mode="partial")
        stored = ProfileSkill.objects.get().evidence
        self.assertEqual(stored["repo_count"], 14)
        self.assertEqual((stored["first_seen"], stored["last_seen"]), ("2019-03", "2026-10"))
        self.assertEqual(stored["repos"], ["a", "b"])

    def test_GHX8_nothing_is_ever_deleted_by_a_reimport(self):
        ps.apply_skills(self.profile.pk, [skill("Python"), skill("Go")])
        ps.apply_skills(self.profile.pk, [skill("Python")])
        self.assertEqual(ProfileSkill.objects.count(), 2)

    def test_GHX8_dismiss_hides_a_skill_and_is_remembered(self):
        ps.apply_skills(self.profile.pk, [skill("Python"), skill("Go")])
        row = ProfileSkill.objects.get(key="go")
        self.assertTrue(ps.dismiss_skill(self.profile, row.pk))
        self.assertFalse(ps.dismiss_skill(self.profile, row.pk))  # already hidden
        self.assertEqual([r.name for r in ps.visible_skills(self.profile)], ["Python"])
        self.assertEqual(ps.dismissed_keys(self.profile), {"go"})
        self.assertEqual(ProfileSkill.objects.count(), 2)

    def test_GHX8_accepting_a_removed_skill_again_brings_it_back(self):
        ps.apply_skills(self.profile.pk, [skill("Go")])
        ps.dismiss_skill(self.profile, ProfileSkill.objects.get().pk)
        ps.apply_skills(self.profile.pk, [skill("Go")])
        self.assertFalse(ProfileSkill.objects.get().dismissed)

    def test_GHX8_dismiss_is_scoped_to_the_owner(self):
        ps.apply_skills(self.profile.pk, [skill("Python")])
        row = ProfileSkill.objects.get()
        self.assertFalse(ps.dismiss_skill(self.other, row.pk))
        self.assertFalse(ProfileSkill.objects.get().dismissed)

    def test_GHX8_one_invalid_skill_writes_nothing(self):
        with self.assertRaises(ps.SkillValueError) as caught:
            ps.apply_skills(self.profile.pk, [skill("Python"), skill("Go", repo_count=0)])
        self.assertEqual(caught.exception.code, "1:bad_evidence")
        self.assertEqual(ProfileSkill.objects.count(), 0)

    def test_GHX8_duplicates_and_oversized_lists_are_refused(self):
        with self.assertRaises(ps.SkillValueError):
            ps.apply_skills(self.profile.pk, [skill("Python"), skill("python")])
        with self.assertRaises(ps.SkillValueError):
            ps.apply_skills(self.profile.pk, [skill(f"Skill{i}") for i in range(41)])

    def test_GHX8_skills_are_isolated_per_profile(self):
        ps.apply_skills(self.profile.pk, [skill("Python")])
        ps.apply_skills(self.other.pk, [skill("Python", repo_count=2)])
        self.assertEqual(ProfileSkill.objects.count(), 2)
        self.assertEqual(len(ps.visible_skills(self.profile)), 1)


class ProjectFieldsTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def apply(self, *entries):
        return apply_entries(self.profile.pk, list(entries), today=TODAY)

    def test_GHX7_description_is_plain_bounded_text(self):
        self.assertEqual(clean_description("  Hello\n <b>world</b>‮  "), "Hello b world /b")
        self.assertLessEqual(len(clean_description("x " * 400)), 300)
        self.assertEqual(clean_description("12345"), "")
        self.assertEqual(clean_description(None), "")
        self.assertEqual(clean_description("a​b"), "a b")

    def test_GHX7_an_entry_url_must_be_an_https_github_link(self):
        self.assertEqual(clean_entry(project(), TODAY)["url"], "https://github.com/alice/side-site")
        for bad in ("http://github.com/a/b", "https://evil.example/a/b", "javascript:alert(1)", "https://user@github.com/a/b"):
            with self.assertRaises(EntryValueError, msg=bad):
                clean_entry(project(url=bad), TODAY)
        self.assertEqual(clean_entry(project(url=""), TODAY)["url"], "")

    def test_GHX7_description_and_url_are_stored_and_a_reimport_is_unchanged(self):
        self.apply(project())
        row = ResumeEntry.objects.get()
        self.assertEqual((row.description, row.url), ("A small site.", "https://github.com/alice/side-site"))
        result = self.apply(project())
        self.assertEqual((len(result.created), len(result.unchanged)), (0, 1))

    def test_GHX7_a_changed_description_updates_an_imported_project(self):
        self.apply(project())
        result = self.apply(project(description="A bigger site."))
        self.assertEqual(len(result.updated), 1)
        self.assertEqual(ResumeEntry.objects.get().description, "A bigger site.")

    def test_GHX7_a_project_is_matched_by_its_link_before_its_title(self):
        self.apply(project())
        result = self.apply(project(title="Side Site v2"))  # repo renamed, same link
        self.assertEqual((len(result.created), len(result.updated)), (0, 1))
        row = ResumeEntry.objects.get()
        self.assertEqual(row.title, "Side Site v2")
        self.assertEqual(ResumeEntry.objects.count(), 1)

    def test_GHX7_a_user_edited_project_found_by_link_is_kept(self):
        self.apply(project(edited=True, title="My Own Name"))
        result = self.apply(project(title="Side Site"))
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(ResumeEntry.objects.get().title, "My Own Name")

    def test_GHX9_projects_and_skills_never_change_years_by_skill(self):
        self.apply(project(skills=["React"]))
        ps.apply_skills(self.profile.pk, [skill("React")])
        self.assertEqual(years_by_skill(self.profile, TODAY), [])
