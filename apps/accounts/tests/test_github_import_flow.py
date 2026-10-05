"""The enriched GitHub import end to end: ownership, throttle, payload, review, apply
(rules GHX0, GHX1, GHX2, GHX6, GHX7, GHX8, GHX9)."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts import tasks
from apps.accounts.importing import github, review, service
from apps.accounts.models import ImportJob, Profile, ProfileSkill, ResumeEntry
from apps.accounts.services import profile_skills
from apps.accounts.tests.test_github_fetch import RoutedSession, full_session, repo
from apps.accounts.tests.test_import_github import FakeResponse, jr

User = get_user_model()
LINK = "https://github.com/octocat"


@override_settings(PROFILE_IMPORT_GITHUB_COOLDOWN_SECONDS=0)
class _Base(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile
        Profile.objects.filter(pk=self.profile.pk).update(github_url=LINK)
        self.profile.refresh_from_db()

    def run_import(self, session=None, token="tok", profile=None, username="octocat"):
        client = github.GitHubClient(session=session or full_session(), token=token)
        with mock.patch("apps.accounts.importing.github.GitHubClient", return_value=client):
            with self.captureOnCommitCallbacks(execute=True):
                job = service.start_github_import(profile or self.profile, username)
        job.refresh_from_db()
        return job

    def post_for(self, job, *, skills=True, entries=False, decisions=None):
        built = review.build_review(job)
        post = {}
        for row in built.fields:
            if row.accept:
                post[f"decision__{row.key}"] = "accept"
                post[f"value__{row.key}"] = row.imported
        for row in built.skills:
            if skills and row.accept:
                post[f"skill_decision__{row.index}"] = "accept"
        for row in built.entries:
            if entries:
                post.update({
                    f"entry_decision__{row.index}": "accept", f"entry_title__{row.index}": row.title,
                    f"entry_org__{row.index}": row.organization, f"entry_start__{row.index}": row.start,
                    f"entry_end__{row.index}": row.end, f"entry_skills__{row.index}": row.skills,
                })
        post.update(decisions or {})
        return post


class OwnershipTests(_Base):
    def test_GHX0_a_profile_without_a_saved_link_is_asked_to_add_one(self):
        Profile.objects.filter(pk=self.other.pk).update(github_url="")
        session = full_session()
        with self.assertRaises(github.GitHubError) as caught:
            self.run_import(session, profile=self.other)
        self.assertEqual(caught.exception.code, "github_link_required")
        self.assertEqual((ImportJob.objects.count(), session.calls), (0, []))
        self.assertIsNone(cache.get(service._rate_key(self.other.user_id, "github")))
        self.assertIn("Add your GitHub link", service.error_message("github_link_required"))

    def test_GHX0_only_the_linked_account_can_be_imported(self):
        session = full_session()
        with self.assertRaises(github.GitHubError) as caught:
            self.run_import(session, username="torvalds")
        self.assertEqual(caught.exception.code, "github_not_your_account")
        self.assertEqual((ImportJob.objects.count(), session.calls), (0, []))

    def test_GHX0_case_and_link_shape_do_not_matter(self):
        for saved in ("https://github.com/OctoCat/", "github.com/octocat", "http://www.github.com/OCTOCAT"):
            cache.clear()
            ImportJob.objects.all().delete()
            Profile.objects.filter(pk=self.profile.pk).update(github_url=saved)
            self.profile.refresh_from_db()
            self.assertEqual(self.run_import(username="octocat").status, "ready", saved)

    def test_GHX0_a_saved_link_that_is_not_a_profile_counts_as_missing(self):
        for saved in ("https://github.com/octocat/repo", "https://example.com/octocat", "not a url"):
            Profile.objects.filter(pk=self.profile.pk).update(github_url=saved)
            self.profile.refresh_from_db()
            with self.assertRaises(github.GitHubError, msg=saved) as caught:
                service.start_github_import(self.profile, "octocat")
            self.assertEqual(caught.exception.code, "github_link_required")


class ThrottleTests(_Base):
    @override_settings(PROFILE_IMPORT_GITHUB_COOLDOWN_SECONDS=300)
    def test_GHX1_a_second_import_inside_the_cooldown_is_refused_per_user(self):
        with mock.patch("apps.accounts.tasks.run_github_import"):
            first = service.start_github_import(self.profile, "octocat")
            ImportJob.objects.filter(pk=first.pk).update(status=ImportJob.Status.APPLIED)
            with self.assertRaises(github.GitHubError) as caught:
                service.start_github_import(self.profile, "octocat")
            self.assertEqual(caught.exception.code, "github_cooldown")
            Profile.objects.filter(pk=self.other.pk).update(github_url=LINK)
            self.other.refresh_from_db()
            service.start_github_import(self.other, "octocat")  # another user is unaffected

    @override_settings(PROFILE_IMPORT_GITHUB_COOLDOWN_SECONDS=300)
    def test_GHX1_a_refused_start_does_not_start_the_cooldown(self):
        with mock.patch("apps.accounts.tasks.run_github_import"):
            with self.assertRaises(github.GitHubError):
                service.start_github_import(self.profile, "torvalds")  # not the linked account
            self.assertIsNone(cache.get(service._cooldown_key(self.profile.user_id)))
            service.start_github_import(self.profile, "octocat")  # still allowed
            self.assertIsNotNone(cache.get(service._cooldown_key(self.profile.user_id)))
        self.assertEqual(ImportJob.objects.count(), 1)

    def test_GHX1_a_nearly_empty_quota_refuses_the_import_before_any_request(self):
        cache.set(service.GITHUB_QUOTA_KEY, 3, 600)
        session = full_session()
        job = self.run_import(session)
        self.assertEqual((job.status, job.error_code), ("failed", "github_rate_limited"))
        self.assertEqual(session.calls + session.posts, [])

    def test_GHX1_a_low_quota_with_a_token_drops_to_light_mode(self):
        cache.set(service.GITHUB_QUOTA_KEY, 100, 600)
        session = full_session()
        job = self.run_import(session)
        self.assertEqual(job.payload["meta"]["mode"], "light")
        self.assertEqual(session.posts, [])

    def test_GHX1_plenty_of_quota_with_a_token_runs_in_full_mode(self):
        cache.set(service.GITHUB_QUOTA_KEY, 4000, 600)
        job = self.run_import(full_session())
        self.assertEqual(job.payload["meta"]["mode"], "full")

    def test_GHX2_without_a_token_the_import_is_light(self):
        session = full_session()
        job = self.run_import(session, token="")
        self.assertEqual((job.status, job.payload["meta"]["mode"]), ("ready", "light"))
        self.assertEqual(session.posts, [])

    def test_GHX1_the_remaining_quota_is_remembered_for_the_next_import(self):
        session = full_session(**{"/users/octocat": jr({"login": "octocat", "html_url": LINK}, headers={"X-RateLimit-Remaining": "1234"})})
        self.run_import(session)
        self.assertEqual(cache.get(service.GITHUB_QUOTA_KEY), 1234)

    def test_GHX1_a_rate_limit_is_remembered_so_other_users_are_not_sent_into_it(self):
        job = self.run_import(RoutedSession({"/users/octocat": FakeResponse(403)}))
        self.assertEqual(job.error_code, "github_rate_limited")
        self.assertEqual(cache.get(service.GITHUB_QUOTA_KEY), 0)


class PayloadTests(_Base):
    def test_GHX6_the_payload_holds_projects_skills_and_the_mode(self):
        job = self.run_import()
        self.assertEqual((job.status, job.payload["v"], job.payload["meta"]["mode"]), ("ready", service.PAYLOAD_VERSION, "full"))
        self.assertEqual(sorted(e["title"] for e in job.payload["entries"]), ["alpha", "beta"])
        names = {s["name"] for s in job.payload["skills"]}
        self.assertTrue({"Python", "Django", "React", "Docker"} <= names)
        self.assertNotIn("github_url", job.payload["fields"])

    def test_GHX7_projects_are_unticked_and_skills_in_two_repos_are_ticked(self):
        built = review.build_review(self.run_import())
        self.assertTrue(built.entries)
        self.assertFalse(any(row.accept for row in built.entries))
        python = next(r for r in built.skills if r.name == "Python")
        self.assertTrue(python.accept)
        self.assertEqual(python.proof, "Python · 2 of 2 recent repos · first 2021 · last 2026")
        self.assertEqual(built.mode, "full")

    def test_GHX6_a_skill_the_user_removed_is_not_ticked_and_says_so(self):
        profile_skills.apply_skills(self.profile.pk, [{"name": "Docker", "origin": "github", "evidence": {
            "repo_count": 1, "scope": "top", "scope_size": 2, "first_seen": "2021-03", "last_seen": "2026-09",
            "repos": ["alpha"], "sources": ["package"]}}])
        profile_skills.dismiss_skill(self.profile, ProfileSkill.objects.get().pk)
        built = review.build_review(self.run_import())
        docker = next(r for r in built.skills if r.name == "Docker")
        self.assertEqual(docker.existing, "removed")
        self.assertFalse(docker.accept)

    def test_GHX2_an_old_payload_without_skills_still_renders(self):
        job = self.run_import()
        ImportJob.objects.filter(pk=job.pk).update(payload={"v": 1, "fields": job.payload["fields"], "entries": [], "meta": {"source": "github"}})
        job.refresh_from_db()
        built = review.build_review(job)
        self.assertEqual((built.skills, built.mode), ([], ""))

    def test_GHX2_a_listing_with_nothing_usable_is_nothing_found(self):
        session = RoutedSession({"/users/octocat": jr({"login": "octocat", "html_url": "https://example.com"}), "/users/octocat/repos": jr([])})
        self.assertEqual(self.run_import(session).error_code, "nothing_found")


class ApplyTests(_Base):
    def test_GHX8_accepted_skills_are_stored_with_their_evidence(self):
        job = self.run_import()
        outcome = review.apply_review(job, self.post_for(job))
        self.assertGreaterEqual(outcome.skills_created, 3)
        python = ProfileSkill.objects.get(profile=self.profile, key="python")
        self.assertEqual(python.evidence["repo_count"], 2)
        self.assertFalse(python.dismissed)
        self.assertEqual(ProfileSkill.objects.filter(profile=self.other).count(), 0)

    def test_GHX7_ticked_projects_are_stored_with_description_url_and_skills(self):
        job = self.run_import()
        review.apply_review(job, self.post_for(job, entries=True))
        row = ResumeEntry.objects.get(profile=self.profile, title="alpha")
        self.assertEqual((row.kind, row.url, row.description), ("project", "https://github.com/octocat/alpha", "A project."))
        self.assertIn("Python", row.skills)
        self.assertEqual(row.source, "imported")

    def test_GHX9_github_skills_and_projects_never_change_years_by_skill(self):
        from apps.accounts.services.resume_facts import years_by_skill

        job = self.run_import()
        review.apply_review(job, self.post_for(job, entries=True))
        self.assertEqual(years_by_skill(self.profile), [])

    def test_GHX8_nothing_ticked_writes_nothing(self):
        job = self.run_import()
        outcome = review.apply_review(job, {})
        self.assertEqual((ProfileSkill.objects.count(), ResumeEntry.objects.count(), outcome.skills_created), (0, 0, 0))

    def test_GHX8_reimporting_unchanged_skills_changes_nothing_and_deletes_nothing(self):
        first = self.run_import()
        review.apply_review(first, self.post_for(first))
        before = {s.key: s.updated_at for s in ProfileSkill.objects.all()}
        second = self.run_import()
        built = review.build_review(second)
        self.assertTrue(all(r.existing == "unchanged" for r in built.skills))
        outcome = review.apply_review(second, self.post_for(second))
        self.assertEqual(outcome.skills_created + outcome.skills_updated, 0)
        self.assertEqual({s.key: s.updated_at for s in ProfileSkill.objects.all()}, before)

    def test_GHX8_a_light_reimport_never_lowers_proof_from_a_full_one(self):
        first = self.run_import()
        review.apply_review(first, self.post_for(first))
        react = ProfileSkill.objects.get(profile=self.profile, key="react")
        self.assertEqual(react.evidence["scope"], "top")
        light = self.run_import(token="")
        post = self.post_for(light)
        review.apply_review(light, post)
        self.assertEqual(ProfileSkill.objects.get(pk=react.pk).evidence, react.evidence)
        self.assertEqual(ProfileSkill.objects.get(profile=self.profile, key="python").evidence["repo_count"], 2)

    def test_GHX8_a_removed_skill_stays_removed_unless_the_user_ticks_it_again(self):
        job = self.run_import()
        review.apply_review(job, self.post_for(job))
        docker = ProfileSkill.objects.get(profile=self.profile, key="docker")
        profile_skills.dismiss_skill(self.profile, docker.pk)
        again = self.run_import()
        review.apply_review(again, self.post_for(again))
        self.assertTrue(ProfileSkill.objects.get(pk=docker.pk).dismissed)
        third = self.run_import()
        row = next(r for r in review.build_review(third).skills if r.name == "Docker")
        review.apply_review(third, {f"skill_decision__{row.index}": "accept"})
        self.assertFalse(ProfileSkill.objects.get(pk=docker.pk).dismissed)

    def test_GHX8_an_invalid_skill_writes_nothing_at_all(self):
        job = self.run_import()
        payload = job.payload
        payload["skills"][0]["evidence"]["repo_count"] = 0
        ImportJob.objects.filter(pk=job.pk).update(payload=payload)
        job.refresh_from_db()
        with self.assertRaises(review.ReviewError) as caught:
            review.apply_review(job, self.post_for(job, entries=True, decisions={"decision__portfolio_url": "accept", "value__portfolio_url": "https://octo.dev"}))
        self.assertTrue(caught.exception.skill_errors)
        profile = Profile.objects.get(pk=self.profile.pk)
        self.assertEqual((ProfileSkill.objects.count(), ResumeEntry.objects.count(), profile.portfolio_url), (0, 0, ""))
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")

    def test_GHX7_a_renamed_repo_resolves_to_the_same_project_and_a_hand_edit_is_kept(self):
        job = self.run_import()
        review.apply_review(job, self.post_for(job, entries=True))
        ResumeEntry.objects.filter(profile=self.profile, title="beta").update(source="user", title="My Own Name")
        again = self.run_import()
        built = review.build_review(again)
        beta = next(r for r in built.entries if r.url.endswith("/beta"))
        self.assertEqual(beta.existing, "kept")
        alpha = next(r for r in built.entries if r.url.endswith("/alpha"))
        self.assertEqual(alpha.existing, "unchanged")
        review.apply_review(again, self.post_for(again, entries=True))
        self.assertEqual(ResumeEntry.objects.filter(profile=self.profile).count(), 2)
        self.assertTrue(ResumeEntry.objects.filter(title="My Own Name").exists())

    def test_GHX7_projects_from_a_second_account_never_touch_another_users_data(self):
        job = self.run_import()
        review.apply_review(job, self.post_for(job, entries=True))
        self.assertEqual(ResumeEntry.objects.filter(profile=self.other).count(), 0)


class TaskWiringTests(_Base):
    def test_the_task_still_runs_the_same_job_only_once(self):
        job = self.run_import()
        with mock.patch("apps.accounts.importing.github.GitHubClient"):
            self.assertIsNone(tasks.run_github_import(str(job.public_id), "octocat"))

    def test_P3_nothing_from_a_response_is_logged(self):
        session = full_session(repos=[repo("alpha", description="ZXQ-SECRET-DESCRIPTION", topics=["ZXQ-SECRET-TOPIC"])])
        with self.assertLogs("apps.accounts.importing.service", level="INFO") as logs:
            self.run_import(session)
        self.assertNotIn("ZXQ-SECRET", "\n".join(logs.output))
