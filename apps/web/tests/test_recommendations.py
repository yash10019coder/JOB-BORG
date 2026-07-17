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

    def _job(self, title="Backend Engineer"):
        self._seq += 1
        return Job.objects.create(
            source_ats="greenhouse", source_job_id=str(self._seq),
            employer=self.employer, title=title, source_url="https://x/1",
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
