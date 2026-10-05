"""The Skills section, mode banners, project folding and the skill remove route
(rules GHX0, GHX2, GHX6-GHX9)."""
from datetime import date

from django.test import Client
from django.urls import reverse

from apps.accounts.importing import github_skills
from apps.accounts.models import ImportJob, Profile, ProfileSkill, ResumeEntry
from apps.accounts.services import profile_skills
from apps.accounts.tests.test_github_skills import repo as gh_repo
from apps.web.tests.test_import_views import _Base

TODAY = date(2026, 10, 5)


def evidence(**extra):
    data = {
        "repo_count": 3, "scope": "top", "scope_size": 8, "first_seen": "2021-03", "last_seen": "2026-09",
        "repos": ["a1"], "sources": ["package"],
    }
    data.update(extra)
    return data


def store(profile, name="React", **extra):
    profile_skills.apply_skills(profile.pk, [{"name": name, "origin": "github", "evidence": evidence(**extra)}])


class GitHubEnrichmentViewTests(_Base):
    def github_job(self, mode="full", repos=None, **extra):
        repos = repos if repos is not None else [gh_repo("alpha"), gh_repo("beta", topics=["django"])]
        derived = github_skills.derive({"user": {"login": "alice"}, "repos": repos}, today=TODAY)
        payload = {
            "v": 2, "fields": {}, "entries": derived["entries"], "skills": derived["skills"],
            "meta": {"source": "github", "mode": mode, "scanned": len(repos)},
        }
        return ImportJob.objects.create(
            profile=self.profile, kind="github", source_kind="github",
            status=ImportJob.Status.READY, payload=payload, **extra,
        )

    def review_page(self, job):
        return self.client.get(reverse("import_review", args=[job.public_id])).content.decode()

    def test_GHX0_the_import_tab_asks_for_a_github_link_first(self):
        page = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn("first add your GitHub link", page)
        self.assertNotIn('id="github-username"', page)

    def test_GHX0_with_a_saved_link_the_form_is_prefilled_with_that_account(self):
        Profile.objects.filter(pk=self.profile.pk).update(github_url="https://github.com/alice")
        page = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn('value="alice"', page)
        self.assertNotIn("first add your GitHub link", page)

    def test_GHX6_the_review_lists_skills_with_proof_and_ticks_those_in_two_repos(self):
        page = self.review_page(self.github_job())
        self.assertIn("Skills found", page)
        self.assertIn("Python · 2 of 2 recent repos", page.replace("&middot;", "·"))
        self.assertRegex(page, r'name="skill_decision__\d+" value="accept" checked')
        self.assertIn("not verified", page)

    def test_GHX7_projects_start_unticked_and_show_their_description_and_link_as_text(self):
        page = self.review_page(self.github_job())
        self.assertIn("Projects found", page)
        self.assertIn("https://github.com/alice/alpha", page)
        self.assertIn("A useful project.", page)
        self.assertNotRegex(page, r'name="entry_decision__\d+" value="accept" checked')
        self.assertNotIn('href="https://github.com/alice/alpha"', page)

    def test_GHX7_a_hostile_description_is_escaped_never_rendered(self):
        job = self.github_job(repos=[gh_repo("alpha", description="<script>alert(1)</script> hello")])
        self.assertNotIn("<script>alert(1)</script>", self.review_page(job))

    def test_GHX7_more_than_ten_projects_fold_behind_a_show_more_control(self):
        page = self.review_page(self.github_job(repos=[gh_repo(f"repo-{i:02d}") for i in range(14)]))
        self.assertEqual(page.count("<details><summary>Show 4 more projects"), 1)
        few = self.review_page(self.github_job(repos=[gh_repo(f"repo-{i:02d}") for i in range(5)]))
        self.assertNotIn("more projects", few)

    def test_GHX2_each_mode_has_its_own_note(self):
        self.assertIn("Without a GitHub access token", self.review_page(self.github_job(mode="light")))
        self.assertIn("GitHub limited requests", self.review_page(self.github_job(mode="partial")))
        self.assertIn("Private work is not included", self.review_page(self.github_job(mode="full")))
        self.assertNotIn("public GitHub activity", self.review_page(self.ready_job()))

    def test_GHX6_a_skill_the_user_removed_is_labelled(self):
        job = self.github_job()
        skill = job.payload["skills"][0]
        profile_skills.apply_skills(self.profile.pk, [{"name": skill["name"], "origin": "github", "evidence": skill["evidence"]}])
        profile_skills.dismiss_skill(self.profile, ProfileSkill.objects.get().pk)
        self.assertIn("you removed this", self.review_page(job))

    def test_GHX8_applying_saves_skills_and_projects_and_says_so(self):
        job = self.github_job()
        entry = job.payload["entries"][0]
        post = {f"skill_decision__{i}": "accept" for i, _ in enumerate(job.payload["skills"])}
        post.update({
            "entry_decision__0": "accept", "entry_title__0": entry["title"], "entry_org__0": "",
            "entry_start__0": entry["start"], "entry_end__0": entry["end"], "entry_skills__0": ", ".join(entry["skills"]),
        })
        response = self.client.post(reverse("import_apply", args=[job.public_id]), post, follow=True)
        self.assertContains(response, "skill")
        self.assertTrue(ProfileSkill.objects.filter(profile=self.profile).exists())
        self.assertTrue(ResumeEntry.objects.get(profile=self.profile).url.startswith("https://github.com/"))
        self.assertEqual(ProfileSkill.objects.filter(profile=self.other).count(), 0)

    def test_GHX8_an_invalid_skill_shows_an_error_and_saves_nothing(self):
        job = self.github_job()
        payload = job.payload
        payload["skills"][0]["evidence"]["repo_count"] = 0
        ImportJob.objects.filter(pk=job.pk).update(payload=payload)
        post = {f"skill_decision__{i}": "accept" for i, _ in enumerate(payload["skills"])}
        response = self.client.post(reverse("import_apply", args=[job.public_id]), post)
        self.assertContains(response, "Nothing was saved")
        self.assertContains(response, "This skill is not valid")
        self.assertEqual(ProfileSkill.objects.count(), 0)

    def test_GHX9_the_import_tab_shows_skills_as_seen_on_github_apart_from_job_years(self):
        store(self.profile, "Python", repo_count=14, scope="recent", scope_size=90, first_seen="2019-03")
        page = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn("Your skills", page)
        self.assertIn("14 of 90 recent repos", page)
        self.assertIn("seen on GitHub since 2019", page)
        self.assertNotIn("Years by skill", page)  # there are no job entries, and GitHub never fills job years

    def test_GHX8_removing_a_skill_hides_it_and_is_owner_scoped_post_only(self):
        store(self.profile)
        store(self.other)
        mine = ProfileSkill.objects.get(profile=self.profile)
        theirs = ProfileSkill.objects.get(profile=self.other)
        url = reverse("import_skill_dismiss", args=[mine.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(reverse("import_skill_dismiss", args=[theirs.pk])).status_code, 404)
        self.assertFalse(ProfileSkill.objects.get(pk=theirs.pk).dismissed)
        self.client.post(url)
        self.assertTrue(ProfileSkill.objects.get(pk=mine.pk).dismissed)
        self.assertNotIn("Your skills", self.client.get(reverse("profile_import")).content.decode())
        self.assertEqual(self.client.post(url).status_code, 404)  # already removed

    def test_GHX8_the_remove_route_needs_login_and_csrf(self):
        store(self.profile)
        url = reverse("import_skill_dismiss", args=[ProfileSkill.objects.get().pk])
        self.assertEqual(Client().post(url).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(url).status_code, 403)
        self.assertFalse(ProfileSkill.objects.get().dismissed)

    def test_GHX7_a_saved_project_shows_its_description_and_link_on_the_import_tab(self):
        ResumeEntry.objects.create(
            profile=self.profile, kind="project", title="Side Site", natural_key="k", skills=["React"],
            description="Does a thing.", url="https://github.com/alice/side-site",
        )
        page = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn("Does a thing.", page)
        self.assertIn("https://github.com/alice/side-site", page)
