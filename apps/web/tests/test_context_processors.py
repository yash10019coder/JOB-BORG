"""Tests for `apps.web.context_processors` (the auto-apply pending-drafts
nav badge)."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.auto_apply.greenhouse_form.field_mapping import FormSchema
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services.drafting import draft_for
from apps.auto_apply.tests.test_drafting_service import FakeFormClient, FakeLLMClient
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


class AutoApplyPendingCountTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self._seq = 0

    def _job(self):
        self._seq += 1
        return Job.objects.create(
            source_ats="greenhouse", source_job_id=str(self._seq),
            source_url=f"https://job-boards.greenhouse.io/acme/jobs/{self._seq}",
            employer=self.employer, title="Backend Engineer",
        )

    def _draft(self, status=AutoApplyDraft.Status.DRAFTED):
        draft = draft_for(
            self.alice, self._job(),
            form_client=FakeFormClient(schema=FormSchema(fields=())),
            llm_client=FakeLLMClient(),
        )
        if status != AutoApplyDraft.Status.DRAFTED:
            draft.status = status
            draft.save(update_fields=["status"])
        return draft

    def test_anonymous_user_gets_no_badge(self):
        response = Client().get(reverse("login"))
        self.assertNotIn("auto_apply_pending_count", response.context)

    def test_no_drafts_omits_the_badge(self):
        client = Client()
        client.force_login(self.alice)
        response = client.get(reverse("recommendations"))
        self.assertEqual(response.context["auto_apply_pending_count"], 0)
        self.assertNotContains(response, "Drafted applications waiting to be sent")

    def test_drafted_count_shown_in_nav(self):
        self._draft()
        self._draft()
        self._draft(status=AutoApplyDraft.Status.APPLIED)  # must not count
        self._draft(status=AutoApplyDraft.Status.STALE)  # must not count

        client = Client()
        client.force_login(self.alice)
        response = client.get(reverse("recommendations"))

        self.assertEqual(response.context["auto_apply_pending_count"], 2)
        self.assertContains(response, ">2<")

    def test_badge_is_per_user(self):
        bob = User.objects.create_user(username="bob", password="pw")
        self._draft()

        client = Client()
        client.force_login(bob)
        response = client.get(reverse("recommendations"))

        self.assertEqual(response.context["auto_apply_pending_count"], 0)
