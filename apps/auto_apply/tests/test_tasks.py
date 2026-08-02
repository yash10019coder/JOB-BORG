"""Tests for `apps.auto_apply.tasks.draft_auto_apply` (U6).

`GreenhouseFormClient` and the default LLM client (`llm.base.get_client`)
are both patched at the `services.drafting` import site so this exercises
the task -> `draft_for` -> `AutoApplyDraft` path end-to-end without ever
touching Playwright or a real LLM vendor. `CELERY_TASK_ALWAYS_EAGER` (test
settings) makes `.delay()` run synchronously, so tasks are simply called
directly here.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.auto_apply.greenhouse_form.field_mapping import FormField, FormSchema, TEXT
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.tasks import draft_auto_apply
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()

SIMPLE_SCHEMA = FormSchema(
    fields=(
        FormField(label="First Name", field_type=TEXT, required=True),
        FormField(label="Last Name", field_type=TEXT, required=True),
        FormField(label="Email", field_type=TEXT, required=True),
    )
)


class FakeFormClient:
    def __init__(self, schema):
        self.schema = schema

    def inspect(self, job_url):
        return self.schema


class FakeLLMClient:
    def infer(self, questions, resume_text, profile):
        return []


class DraftAutoApplyTaskTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="pw", email="alice@example.com"
        )
        self.user.profile.full_name = "Alice Smith"
        self.user.profile.save()
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = Job.objects.create(
            source_ats="greenhouse",
            source_job_id="1",
            source_url="https://job-boards.greenhouse.io/acme/jobs/1",
            employer=self.employer,
            title="Backend Engineer",
        )


class DraftAutoApplyEndToEndTests(DraftAutoApplyTaskTestCase):
    @patch("apps.auto_apply.services.drafting.llm_base.get_client")
    @patch("apps.auto_apply.services.drafting.GreenhouseFormClient")
    def test_triggering_task_creates_exactly_one_draft_with_expected_answers(
        self, mock_form_client_cls, mock_get_client
    ):
        mock_form_client_cls.return_value = FakeFormClient(SIMPLE_SCHEMA)
        mock_get_client.return_value = FakeLLMClient()

        result = draft_auto_apply(self.user.pk, self.job.pk)

        drafts = AutoApplyDraft.objects.filter(user=self.user, job=self.job)
        self.assertEqual(drafts.count(), 1)
        draft = drafts.get()
        self.assertEqual(result, draft.pk)
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(draft.answers["First Name"]["value"], "Alice")
        self.assertEqual(draft.answers["Last Name"]["value"], "Smith")
        self.assertEqual(draft.answers["Email"]["value"], "alice@example.com")

    @patch("apps.auto_apply.services.drafting.llm_base.get_client")
    @patch("apps.auto_apply.services.drafting.GreenhouseFormClient")
    def test_task_runs_via_delay_under_eager_celery(self, mock_form_client_cls, mock_get_client):
        mock_form_client_cls.return_value = FakeFormClient(SIMPLE_SCHEMA)
        mock_get_client.return_value = FakeLLMClient()

        async_result = draft_auto_apply.delay(self.user.pk, self.job.pk)

        self.assertEqual(AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 1)
        self.assertIsNotNone(async_result.result)


class DraftAutoApplyConcurrentTriggerTests(DraftAutoApplyTaskTestCase):
    @patch("apps.auto_apply.services.drafting.llm_base.get_client")
    @patch("apps.auto_apply.services.drafting.GreenhouseFormClient")
    def test_concurrent_double_trigger_does_not_crash_the_task(
        self, mock_form_client_cls, mock_get_client
    ):
        mock_form_client_cls.return_value = FakeFormClient(SIMPLE_SCHEMA)
        mock_get_client.return_value = FakeLLMClient()
        # Simulate a draft that's already in flight for this (user, job).
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )

        result = draft_auto_apply(self.user.pk, self.job.pk)

        self.assertIsNone(result)
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 1
        )


class DraftAutoApplyErrorIsolationTests(DraftAutoApplyTaskTestCase):
    def test_unknown_user_id_is_caught_and_logged_not_raised(self):
        result = draft_auto_apply(999999, self.job.pk)
        self.assertIsNone(result)

    def test_unknown_job_id_is_caught_and_logged_not_raised(self):
        result = draft_auto_apply(self.user.pk, 999999)
        self.assertIsNone(result)

    @patch("apps.auto_apply.services.drafting.llm_base.get_client")
    @patch("apps.auto_apply.services.drafting.GreenhouseFormClient")
    def test_non_greenhouse_job_is_caught_and_logged_not_raised(
        self, mock_form_client_cls, mock_get_client
    ):
        self.job.source_ats = "lever"
        self.job.save()
        mock_form_client_cls.return_value = FakeFormClient(SIMPLE_SCHEMA)
        mock_get_client.return_value = FakeLLMClient()

        result = draft_auto_apply(self.user.pk, self.job.pk)

        self.assertIsNone(result)
        self.assertEqual(AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 0)
