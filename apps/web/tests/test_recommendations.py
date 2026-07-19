from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.applications.models import JobApplication
from apps.employers.models import Employer
from apps.jobs.models import Job
from apps.matching.constants import MatchStatus
from apps.matching.models import UserJobMatch

User = get_user_model()


class RecommendationsViewTests(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")
        self._seq = 0

    def _job(self, title="Backend Engineer", description=""):
        self._seq += 1
        return Job.objects.create(
            source_ats="greenhouse", source_job_id=str(self._seq),
            employer=self.employer, title=title, description=description,
            source_url="https://x/1",
        )

    def _match(self, user, job, score, status=MatchStatus.RECOMMENDED, tags=None):
        return UserJobMatch.objects.create(
            user=user, job=job, match_score=score,
            match_status=status, matched_tags=tags or [],
        )

    def test_ranked_recommended_only_for_logged_in_user(self):
        j_hi, j_lo = self._job("High"), self._job("Low")
        self._match(self.alice, j_hi, 0.9, tags=["python"])
        self._match(self.alice, j_lo, 0.5)
        # Bob's match must never appear for Alice.
        j_bob = self._job("Bob only")
        self._match(self.bob, j_bob, 0.95)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"))
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertEqual(titles, ["High", "Low"])  # descending score
        self.assertNotIn("Bob only", titles)

    def test_below_threshold_never_shown(self):
        visible = self._job("Visible")
        hidden = self._job("Hidden")
        self._match(self.alice, visible, 0.8)
        self._match(self.alice, hidden, 0.2, status=MatchStatus.BELOW_THRESHOLD)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"))
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertEqual(titles, ["Visible"])

    def test_show_all_toggle_includes_below_threshold(self):
        rec = self._job("Recommended")
        low = self._job("Low")
        self._match(self.alice, rec, 0.8)
        self._match(self.alice, low, 0.2, status=MatchStatus.BELOW_THRESHOLD)

        self.client.force_login(self.alice)
        # Default: recommended only.
        default = self.client.get(reverse("recommendations"))
        self.assertEqual([m.job.title for m in default.context["page_obj"]], ["Recommended"])
        # ?all=1: everything, ranked.
        all_resp = self.client.get(reverse("recommendations"), {"all": "1"})
        self.assertEqual(
            [m.job.title for m in all_resp.context["page_obj"]], ["Recommended", "Low"]
        )
        self.assertTrue(all_resp.context["show_all"])

    def test_show_all_still_excludes_dismissed(self):
        low = self._job("Low")
        self._match(self.alice, low, 0.2, status=MatchStatus.BELOW_THRESHOLD)
        self.client.force_login(self.alice)
        self.client.post(reverse("job_action", args=[low.id]), {"action": "dismiss"})

        resp = self.client.get(reverse("recommendations"), {"all": "1"})
        self.assertEqual([m.job.title for m in resp.context["page_obj"]], [])

    def test_search_matches_title(self):
        py = self._job("Python Backend Engineer")
        other = self._job("Frontend Designer")
        self._match(self.alice, py, 0.9)
        self._match(self.alice, other, 0.8)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertEqual(titles, ["Python Backend Engineer"])
        self.assertEqual(resp.context["query"], "python")

    def test_search_matches_description_only(self):
        job = self._job("Engineer", description="Deep experience with kubernetes clusters.")
        other = self._job("Other Engineer", description="No mention of that word.")
        self._match(self.alice, job, 0.9)
        self._match(self.alice, other, 0.8)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"), {"q": "kubernetes"})
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertEqual(titles, ["Engineer"])

    def test_search_stems_and_is_case_insensitive(self):
        # Postgres FTS stems "Engineering" to match a search for "engineer" —
        # a plain icontains substring match would not do this, so this proves
        # the FTS config is actually wired up rather than a lucky substring hit.
        job = self._job("Senior Engineering Manager")
        self._match(self.alice, job, 0.9)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"), {"q": "ENGINEER"})
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertEqual(titles, ["Senior Engineering Manager"])

    def test_empty_query_behaves_identically_to_no_search(self):
        job = self._job("Backend Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        no_param = self.client.get(reverse("recommendations"))
        empty_q = self.client.get(reverse("recommendations"), {"q": ""})
        self.assertEqual(
            [m.job.title for m in no_param.context["page_obj"]],
            [m.job.title for m in empty_q.context["page_obj"]],
        )
        self.assertEqual(empty_q.context["query"], "")

    def test_search_layers_on_top_of_toggle_not_bypasses_it(self):
        rec = self._job("Python Recommended")
        low = self._job("Python Below Threshold")
        self._match(self.alice, rec, 0.9)
        self._match(self.alice, low, 0.2, status=MatchStatus.BELOW_THRESHOLD)
        self.client.force_login(self.alice)

        # Recommended-only + search: below-threshold match excluded even
        # though it matches the search term — toggle still governs status.
        default = self.client.get(reverse("recommendations"), {"q": "python"})
        self.assertEqual(
            [m.job.title for m in default.context["page_obj"]], ["Python Recommended"]
        )
        # Show-all + search: both matching the term are returned.
        all_resp = self.client.get(reverse("recommendations"), {"q": "python", "all": "1"})
        self.assertEqual(
            [m.job.title for m in all_resp.context["page_obj"]],
            ["Python Recommended", "Python Below Threshold"],
        )

    def test_search_still_excludes_dismissed(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)
        self.client.post(reverse("job_action", args=[job.id]), {"action": "dismiss"})

        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        self.assertEqual([m.job.title for m in resp.context["page_obj"]], [])

    def test_search_returns_no_results_without_error(self):
        job = self._job("Backend Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "nonexistentzzz"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["page_obj"]), [])

    def test_search_isolated_per_user(self):
        job_alice = self._job("Python Engineer")
        job_bob = self._job("Python Engineer")
        self._match(self.alice, job_alice, 0.9)
        self._match(self.bob, job_bob, 0.9)

        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        matches = list(resp.context["page_obj"])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].job_id, job_alice.id)

    def test_search_strips_nul_bytes_instead_of_erroring(self):
        # Postgres text columns reject NUL bytes outright (DataError, not a
        # validation error) -- a raw %00 in ?q= must not 500 the page.
        job = self._job("Backend Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "back\x00end"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["query"], "backend")

    def test_job_action_redirect_preserves_search_and_toggle(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.2, status=MatchStatus.BELOW_THRESHOLD)
        self.client.force_login(self.alice)

        resp = self.client.post(
            reverse("job_action", args=[job.id]),
            {"action": "save", "q": "python", "all": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("q=python", resp["Location"])
        self.assertIn("all=1", resp["Location"])

    def test_job_action_redirect_omits_empty_search_and_toggle(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.post(
            reverse("job_action", args=[job.id]), {"action": "save"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("recommendations"))

    def test_job_action_redirect_preserves_toggle_only(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.2, status=MatchStatus.BELOW_THRESHOLD)
        self.client.force_login(self.alice)

        resp = self.client.post(
            reverse("job_action", args=[job.id]), {"action": "save", "all": "1"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{reverse('recommendations')}?all=1")

    def test_job_action_redirect_preserves_search_only(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.post(
            reverse("job_action", args=[job.id]), {"action": "save", "q": "python"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{reverse('recommendations')}?q=python")

    def test_search_input_prefilled_with_query(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        self.assertContains(resp, 'value="python"')

    def test_toggle_link_preserves_search(self):
        job = self._job("Python Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        self.assertContains(resp, "all=1")
        self.assertContains(resp, "q=python")

    def test_pagination_preserves_search(self):
        for i in range(25):
            job = self._job(f"Python Engineer {i}")
            self._match(self.alice, job, 0.9 - i * 0.01)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "python"})
        self.assertContains(resp, "Next")
        self.assertContains(resp, "q=python")

    def test_search_empty_state_renders_distinct_message(self):
        job = self._job("Backend Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(reverse("recommendations"), {"q": "nonexistentzzz"})
        self.assertContains(resp, "No matches found for")
        self.assertNotContains(resp, "No recommendations above the threshold yet")

    def test_search_empty_state_omits_show_all_suggestion_when_already_all(self):
        job = self._job("Backend Engineer")
        self._match(self.alice, job, 0.9)
        self.client.force_login(self.alice)

        resp = self.client.get(
            reverse("recommendations"), {"q": "nonexistentzzz", "all": "1"}
        )
        self.assertContains(resp, "No matches found for")
        self.assertNotContains(resp, "showing all matches")

    def test_matched_tags_explanation_rendered(self):
        job = self._job("Tagged")
        self._match(self.alice, job, 0.8, tags=["python", "kubernetes"])
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"))
        self.assertContains(resp, "python")
        self.assertContains(resp, "kubernetes")

    def test_empty_state_renders_friendly(self):
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("recommendations"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No recommendations above the threshold yet")

    def test_save_creates_saved_application(self):
        job = self._job()
        self._match(self.alice, job, 0.8)
        self.client.force_login(self.alice)
        self.client.post(reverse("job_action", args=[job.id]), {"action": "save"})
        app = JobApplication.objects.get(user=self.alice, job=job)
        self.assertEqual(app.status, JobApplication.Status.SAVED)

    def test_dismiss_removes_job_from_list(self):
        job = self._job("Dismissed")
        self._match(self.alice, job, 0.8)
        self.client.force_login(self.alice)
        self.client.post(reverse("job_action", args=[job.id]), {"action": "dismiss"})
        resp = self.client.get(reverse("recommendations"))
        titles = [m.job.title for m in resp.context["page_obj"]]
        self.assertNotIn("Dismissed", titles)

    def test_mark_applied_is_idempotent(self):
        job = self._job()
        self._match(self.alice, job, 0.8)
        self.client.force_login(self.alice)
        for _ in range(3):
            self.client.post(reverse("job_action", args=[job.id]), {"action": "apply"})
        self.assertEqual(
            JobApplication.objects.filter(user=self.alice, job=job).count(), 1
        )
        self.assertEqual(
            JobApplication.objects.get(user=self.alice, job=job).status,
            JobApplication.Status.APPLIED,
        )

    def test_save_then_applied_updates_same_row(self):
        job = self._job()
        self._match(self.alice, job, 0.8)
        self.client.force_login(self.alice)
        self.client.post(reverse("job_action", args=[job.id]), {"action": "save"})
        self.client.post(reverse("job_action", args=[job.id]), {"action": "apply"})
        self.assertEqual(
            JobApplication.objects.filter(user=self.alice, job=job).count(), 1
        )
        self.assertEqual(
            JobApplication.objects.get(user=self.alice, job=job).status,
            JobApplication.Status.APPLIED,
        )

    def test_user_cannot_act_on_another_users_data(self):
        job = self._job()
        self._match(self.bob, job, 0.9)  # only Bob has a match
        self.client.force_login(self.alice)
        # Alice can POST an action, but it only creates HER application, never
        # touching Bob's data.
        self.client.post(reverse("job_action", args=[job.id]), {"action": "save"})
        self.assertFalse(JobApplication.objects.filter(user=self.bob).exists())
        self.assertEqual(JobApplication.objects.filter(user=self.alice).count(), 1)

    def test_anonymous_action_redirects_to_login(self):
        job = self._job()
        resp = self.client.post(reverse("job_action", args=[job.id]), {"action": "save"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_action_rejects_get(self):
        job = self._job()
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("job_action", args=[job.id]))
        self.assertEqual(resp.status_code, 405)  # require_POST

    def test_action_rejects_invalid_csrf(self):
        job = self._job()
        self._match(self.alice, job, 0.8)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        resp = csrf_client.post(
            reverse("job_action", args=[job.id]), {"action": "save"}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(JobApplication.objects.filter(user=self.alice).exists())
