"""Tests for `apps.auto_apply.tasks` (U6's `draft_auto_apply`, U7's
`submit_auto_apply_draft` and `sweep_stale_auto_apply_drafts`).

`GreenhouseFormClient` and the default LLM client (`llm.base.get_client`)
are both patched at the `services.drafting` import site so `draft_auto_apply`
tests exercise the task -> `draft_for` -> `AutoApplyDraft` path end-to-end
without ever touching Playwright or a real LLM vendor. `submit_auto_apply_draft`
tests patch `GreenhouseFormClient` at the `apps.auto_apply.tasks` import
site instead. `CELERY_TASK_ALWAYS_EAGER` (test settings) makes `.delay()`
run synchronously, so tasks are simply called directly here.
"""
from datetime import timedelta
from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.applications.models import JobApplication
from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormSchemaMismatch,
)
from apps.auto_apply.greenhouse_form.field_mapping import (
    FormField,
    FormSchema,
    SubmissionResult,
    TEXT,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.tasks import (
    draft_auto_apply,
    submit_auto_apply_draft,
    sweep_stale_auto_apply_drafts,
)
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

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
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


# -- submit_auto_apply_draft (U7) --------------------------------------------

ANSWERS_PAYLOAD = {
    "First Name": {"value": "Alice", "needs_review": False, "category": "standard", "reason": "profile"},
    "Email": {"value": "alice@example.com", "needs_review": False, "category": "standard", "reason": "profile"},
}


class SubmitAutoApplyDraftTaskTestCase(TestCase):
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
            status=Job.Status.OPEN,
        )

    def _make_draft(self, status=AutoApplyDraft.Status.SENDING, answers=None):
        return AutoApplyDraft.objects.create(
            user=self.user,
            job=self.job,
            status=status,
            answers=answers if answers is not None else ANSWERS_PAYLOAD,
        )


class SubmitAutoApplyDraftSuccessTests(SubmitAutoApplyDraftTaskTestCase):
    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_successful_submit_transitions_to_applied_and_creates_job_application(
        self, mock_client_cls
    ):
        mock_client_cls.return_value.submit.return_value = SubmissionResult(success=True)
        draft = self._make_draft()

        result = submit_auto_apply_draft(draft.pk)

        draft.refresh_from_db()
        self.assertEqual(result, draft.pk)
        self.assertEqual(draft.status, AutoApplyDraft.Status.APPLIED)
        self.assertIsNotNone(draft.job_application)
        self.assertEqual(draft.job_application.status, JobApplication.Status.APPLIED)
        job_application = JobApplication.objects.get(user=self.user, job=self.job)
        self.assertEqual(job_application.status, JobApplication.Status.APPLIED)

        # submit() must be called with plain label -> value, not the full
        # {value, needs_review, category, reason} answer-metadata dict.
        mock_client_cls.return_value.submit.assert_called_once_with(
            self.job.source_url,
            {"First Name": "Alice", "Email": "alice@example.com"},
            expected_schema=None,
            captcha_solver=None,
            email_code_provider=None,
            deadline_monotonic=ANY,

        )


    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_existing_saved_job_application_is_upserted_to_applied(self, mock_client_cls):
        """Regression test: a JobApplication may already exist in Saved status
        from the user's prior manual action -- `update_or_create` (not
        `get_or_create`) must flip that existing row to Applied rather than
        silently leaving it untouched."""
        existing = JobApplication.objects.create(
            user=self.user, job=self.job, status=JobApplication.Status.SAVED
        )
        mock_client_cls.return_value.submit.return_value = SubmissionResult(success=True)
        draft = self._make_draft()

        submit_auto_apply_draft(draft.pk)

        existing.refresh_from_db()
        self.assertEqual(existing.status, JobApplication.Status.APPLIED)
        self.assertEqual(JobApplication.objects.filter(user=self.user, job=self.job).count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.job_application_id, existing.pk)
        self.assertEqual(draft.status, AutoApplyDraft.Status.APPLIED)

    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_existing_dismissed_job_application_is_upserted_to_applied(self, mock_client_cls):
        existing = JobApplication.objects.create(
            user=self.user, job=self.job, status=JobApplication.Status.DISMISSED
        )
        mock_client_cls.return_value.submit.return_value = SubmissionResult(success=True)
        draft = self._make_draft()

        submit_auto_apply_draft(draft.pk)

        existing.refresh_from_db()
        self.assertEqual(existing.status, JobApplication.Status.APPLIED)

    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_mid_transaction_failure_leaves_neither_write_committed(self, mock_client_cls):
        mock_client_cls.return_value.submit.return_value = SubmissionResult(success=True)
        draft = self._make_draft()

        # Force the second write inside the atomic block to blow up after the
        # JobApplication upsert has already happened in-transaction. The
        # draft's status transition is now a `QuerySet.update()` (see
        # tasks.py), not `AutoApplyDraft.save()`, so that's what must be
        # patched to simulate the failure.
        with patch(
            "django.db.models.query.QuerySet.update",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                submit_auto_apply_draft(draft.pk)

        # Neither the JobApplication upsert nor the draft status change
        # should have survived the rollback.
        self.assertFalse(JobApplication.objects.filter(user=self.user, job=self.job).exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)

    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_draft_reset_by_sweep_mid_submission_leaves_status_alone(self, mock_client_cls):
        """Regression test: if `sweep_stale_auto_apply_drafts` resets a
        draft's status away from SENDING (stuck-SENDING recovery) while this
        submission was still genuinely in flight, the success path's
        conditional `.update(...pk=draft.pk, status=SENDING...)` must not
        clobber that back to APPLIED -- the JobApplication write is still
        the source of truth for the real outcome, but the draft itself
        should be left exactly as the sweep left it."""
        draft = self._make_draft()

        def _race_then_succeed(*args, **kwargs):
            # Simulate the sweep racing in and marking the draft FAILED
            # (stuck-SENDING recovery) while this submit() call -- the slow,
            # real browser-automation step -- is still in flight.
            AutoApplyDraft.objects.filter(pk=draft.pk).update(
                status=AutoApplyDraft.Status.FAILED,
                error_message="Submission timed out / recovered from stuck SENDING state.",
            )
            return SubmissionResult(success=True)

        mock_client_cls.return_value.submit.side_effect = _race_then_succeed

        result = submit_auto_apply_draft(draft.pk)

        self.assertEqual(result, draft.pk)
        draft.refresh_from_db()
        # Left as the sweep set it -- not clobbered back to APPLIED.
        self.assertEqual(draft.status, AutoApplyDraft.Status.FAILED)
        # But the JobApplication write itself still happened -- it's the
        # source of truth for the real-world outcome regardless of the
        # draft's bookkeeping status.
        job_application = JobApplication.objects.get(user=self.user, job=self.job)
        self.assertEqual(job_application.status, JobApplication.Status.APPLIED)


class SubmitAutoApplyDraftStalenessTests(SubmitAutoApplyDraftTaskTestCase):
    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_job_closed_before_send_marks_draft_stale_without_submitting(self, mock_client_cls):
        self.job.status = Job.Status.CLOSED
        self.job.save()
        draft = self._make_draft()

        result = submit_auto_apply_draft(draft.pk)

        draft.refresh_from_db()
        self.assertEqual(result, draft.pk)
        self.assertEqual(draft.status, AutoApplyDraft.Status.STALE)
        mock_client_cls.return_value.submit.assert_not_called()
        self.assertFalse(JobApplication.objects.filter(user=self.user, job=self.job).exists())


class SubmitAutoApplyDraftFailureTests(SubmitAutoApplyDraftTaskTestCase):
    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_greenhouse_form_challenged_marks_failed_with_error_message(self, mock_client_cls):
        mock_client_cls.return_value.submit.side_effect = GreenhouseFormChallenged(
            "CAPTCHA solve failed."
        )
        draft = self._make_draft()

        result = submit_auto_apply_draft(draft.pk)

        draft.refresh_from_db()
        self.assertEqual(result, draft.pk)
        self.assertEqual(draft.status, AutoApplyDraft.Status.FAILED)
        self.assertIn("CAPTCHA solve failed", draft.error_message)
        self.assertFalse(JobApplication.objects.filter(user=self.user, job=self.job).exists())
        self.assertIsNone(draft.job_application)

    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_schema_mismatch_marks_failed_with_distinct_error_message(self, mock_client_cls):
        mock_client_cls.return_value.submit.side_effect = GreenhouseFormSchemaMismatch(
            "Rendered schema has drifted since drafting."
        )
        draft = self._make_draft()

        submit_auto_apply_draft(draft.pk)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.FAILED)
        self.assertIn("drifted", draft.error_message)

    def test_challenged_and_schema_mismatch_produce_different_error_messages(self):
        # Sanity check that the two error-path test scenarios above actually
        # assert distinguishable messages, per the plan's explicit call-out.
        self.assertNotEqual(
            "CAPTCHA solve failed.", "Rendered schema has drifted since drafting."
        )


class SubmitAutoApplyDraftDefensiveNoOpTests(SubmitAutoApplyDraftTaskTestCase):
    @patch("apps.auto_apply.tasks.GreenhouseFormClient")
    def test_draft_not_sending_is_a_defensive_noop(self, mock_client_cls):
        draft = self._make_draft(status=AutoApplyDraft.Status.FAILED)

        result = submit_auto_apply_draft(draft.pk)

        self.assertIsNone(result)
        mock_client_cls.return_value.submit.assert_not_called()
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.FAILED)

    def test_missing_draft_is_a_defensive_noop(self):
        result = submit_auto_apply_draft(999999)
        self.assertIsNone(result)


# -- sweep_stale_auto_apply_drafts (U7) --------------------------------------


class SweepStaleAutoApplyDraftsTests(SubmitAutoApplyDraftTaskTestCase):
    def test_sweep_marks_drafted_drafts_of_closed_jobs_stale_only(self):
        open_job_draft = AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )

        closed_employer = Employer.objects.create(name="Closed Co", slug="closed-co")
        closed_job = Job.objects.create(
            source_ats="greenhouse",
            source_job_id="2",
            source_url="https://job-boards.greenhouse.io/closedco/jobs/2",
            employer=closed_employer,
            title="Frontend Engineer",
            status=Job.Status.CLOSED,
        )
        other_user = User.objects.create_user(
            username="bob", password="pw", email="bob@example.com"
        )
        closed_job_draft = AutoApplyDraft.objects.create(
            user=other_user, job=closed_job, status=AutoApplyDraft.Status.DRAFTED
        )

        stats = sweep_stale_auto_apply_drafts()

        open_job_draft.refresh_from_db()
        closed_job_draft.refresh_from_db()
        self.assertEqual(open_job_draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(closed_job_draft.status, AutoApplyDraft.Status.STALE)
        self.assertEqual(stats["stale"], 1)

    @override_settings(AUTO_APPLY_SENDING_TIMEOUT_SECONDS=300)
    def test_sweep_recovers_stuck_sending_draft_past_timeout(self):
        stuck_draft = self._make_draft(status=AutoApplyDraft.Status.SENDING)
        AutoApplyDraft.objects.filter(pk=stuck_draft.pk).update(
            updated_at=timezone.now() - timedelta(seconds=400)
        )
        # A different (user, job) pair -- the conditional unique constraint
        # only allows one non-terminal (DRAFTED/SENDING) draft per (user, job).
        other_employer = Employer.objects.create(name="Other Co", slug="other-co")
        other_job = Job.objects.create(
            source_ats="greenhouse",
            source_job_id="99",
            source_url="https://job-boards.greenhouse.io/otherco/jobs/99",
            employer=other_employer,
            title="QA Engineer",
            status=Job.Status.OPEN,
        )
        fresh_sending_draft = AutoApplyDraft.objects.create(
            user=self.user, job=other_job, status=AutoApplyDraft.Status.SENDING
        )

        stats = sweep_stale_auto_apply_drafts()

        stuck_draft.refresh_from_db()
        fresh_sending_draft.refresh_from_db()
        self.assertEqual(stuck_draft.status, AutoApplyDraft.Status.FAILED)
        self.assertIn("timed out", stuck_draft.error_message.lower())
        self.assertEqual(fresh_sending_draft.status, AutoApplyDraft.Status.SENDING)
        self.assertEqual(stats["recovered_sending"], 1)
