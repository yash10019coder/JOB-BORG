"""Tests for the auto-apply web views (U8): trigger, queue, edit, send.

`draft_auto_apply`/`submit_auto_apply_draft` are patched at the
`apps.web.views` import site so these tests exercise only the view layer
(request handling, ownership scoping, redirects) without needing a real
`GreenhouseFormClient`/LLM client -- `CELERY_TASK_ALWAYS_EAGER` (test
settings) would otherwise run the real task body synchronously via
`.delay()`.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.applications.models import JobApplication
from apps.auto_apply.greenhouse_form.field_mapping import (
    CHECKBOX_GROUP, COMBOBOX_SELECT, FILE, MULTI_SELECT, SINGLE_SELECT, TEXT,
    FormField, FormSchema, schema_to_dict,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services.drafting import draft_for
from apps.auto_apply.tests.test_drafting_service import FakeFormClient, FakeLLMClient
from apps.web.views import _blocking_required_fields
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


class AutoApplyViewsTestCase(TestCase):
    def setUp(self):
        # Profile-save triggers a debounced rematch signal in apps.matching;
        # not relevant here and not worth a real Celery round-trip.
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")
        self._seq = 0

    def _job(self, ats="greenhouse"):
        self._seq += 1
        return Job.objects.create(
            source_ats=ats,
            source_job_id=str(self._seq),
            employer=self.employer,
            title="Backend Engineer",
            source_url="https://job-boards.greenhouse.io/acme/jobs/1",
        )

    def _draft(self, user, job, status=AutoApplyDraft.Status.DRAFTED, **kwargs):
        return AutoApplyDraft.objects.create(user=user, job=job, status=status, **kwargs)

    def _client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class TriggerAutoApplyTests(AutoApplyViewsTestCase):
    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_enqueues_task_and_redirects_with_message(self, mock_task):
        job = self._job()
        client = self._client_for(self.alice)

        response = client.post(reverse("trigger_auto_apply", args=[job.id]), follow=True)

        mock_task.delay.assert_called_once_with(self.alice.id, job.id)
        self.assertRedirects(response, reverse("recommendations"))
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("drafting" in m.lower() for m in messages))
        # Message must not promise a specific outcome.
        self.assertFalse(any("applied" in m.lower() for m in messages))

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_with_next_queue_redirects_to_queue_not_recommendations(self, mock_task):
        """The auto-apply queue's "Retry" button posts next=queue so a
        retriggered FAILED draft lands the user back in the queue instead
        of bouncing them to recommendations."""
        job = self._job()
        client = self._client_for(self.alice)

        response = client.post(
            reverse("trigger_auto_apply", args=[job.id]), {"next": "queue"}, follow=True
        )

        mock_task.delay.assert_called_once_with(self.alice.id, job.id)
        self.assertRedirects(response, reverse("auto_apply_queue"))

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_rejects_non_greenhouse_job(self, mock_task):
        job = self._job(ats="lever")
        client = self._client_for(self.alice)

        response = client.post(reverse("trigger_auto_apply", args=[job.id]))

        self.assertEqual(response.status_code, 400)
        mock_task.delay.assert_not_called()

    def test_trigger_requires_login(self):
        job = self._job()
        response = Client().post(reverse("trigger_auto_apply", args=[job.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_requires_post(self, mock_task):
        job = self._job()
        client = self._client_for(self.alice)
        response = client.get(reverse("trigger_auto_apply", args=[job.id]))
        self.assertEqual(response.status_code, 405)
        mock_task.delay.assert_not_called()

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_refuses_to_re_apply_when_job_application_already_applied(self, mock_task):
        """Regression test: a real JobApplication already exists in Applied
        status for this (user, job) -- re-triggering must not enqueue a
        second real submission to the same employer."""
        job = self._job()
        JobApplication.objects.create(
            user=self.alice, job=job, status=JobApplication.Status.APPLIED
        )
        client = self._client_for(self.alice)

        response = client.post(reverse("trigger_auto_apply", args=[job.id]), follow=True)

        mock_task.delay.assert_not_called()
        self.assertRedirects(response, reverse("recommendations"))
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("already applied" in m.lower() for m in messages))

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_trigger_refuses_to_re_apply_when_draft_already_applied(self, mock_task):
        """Same guard, but via an AutoApplyDraft already in Applied status
        rather than the JobApplication directly -- both are checked since
        either can exist independently of the other in edge cases."""
        job = self._job()
        self._draft(self.alice, job, status=AutoApplyDraft.Status.APPLIED)
        client = self._client_for(self.alice)

        response = client.post(reverse("trigger_auto_apply", args=[job.id]), follow=True)

        mock_task.delay.assert_not_called()
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("already applied" in m.lower() for m in messages))


class AutoApplyQueueViewTests(AutoApplyViewsTestCase):
    def test_queue_empty_renders_without_error(self):
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No auto-apply drafts yet")

    def test_queue_lists_drafts_across_all_statuses(self):
        job1, job2, job3 = self._job(), self._job(), self._job()
        self._draft(self.alice, job1, status=AutoApplyDraft.Status.DRAFTED)
        self._draft(self.alice, job2, status=AutoApplyDraft.Status.EXCLUDED,
                    exclusion_reason="Required question(s) could not be answered: Visa status")
        self._draft(self.alice, job3, status=AutoApplyDraft.Status.STALE)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertEqual(response.status_code, 200)
        drafts_in_page = list(response.context["page_obj"])
        self.assertEqual(len(drafts_in_page), 3)

    def test_queue_shows_retry_button_only_for_failed_drafts(self):
        job1, job2 = self._job(), self._job()
        self._draft(
            self.alice, job1, status=AutoApplyDraft.Status.FAILED,
            reason_code=AutoApplyDraft.ReasonCode.SUBMISSION_FAILED,
            error_message="boom",
        )
        self._draft(self.alice, job2, status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertContains(response, "Retry")
        self.assertContains(
            response, reverse("trigger_auto_apply", args=[job1.id])
        )
        self.assertNotContains(
            response, reverse("trigger_auto_apply", args=[job2.id])
        )

    def test_queue_distinguishes_needs_review_visually_and_textually(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "Email": {"value": "a@x.com", "needs_review": False, "category": "standard", "reason": "profile"},
                "Why us?": {"value": "Because...", "needs_review": True, "category": "other", "reason": "llm_low_confidence"},
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        # Text label reaches screen readers via aria-label, not color alone.
        self.assertIn("aria-label=\"Needs review", content)
        self.assertIn("Needs review</span>", content)
        # The visual treatment (border-left color) differs for the flagged answer.
        self.assertIn("#f59e0b", content)

    def test_queue_renders_answer_required_badge_and_hides_send_button(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "Work authorization?": {
                    "value": "", "needs_review": True, "required": True,
                    "category": "work_authorization", "reason": "hard_excluded_category",
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        self.assertIn("Answer required</span>", content)
        self.assertIn(
            "aria-label=\"This question must be answered before the application can be sent",
            content,
        )
        self.assertNotIn("Send application", content)
        self.assertIn("Answer 1 required question", content)

    def test_queue_shows_send_button_when_no_blocking_fields(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "Email": {
                    "value": "a@x.com", "needs_review": False, "required": True,
                    "category": "standard", "reason": "profile",
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        self.assertIn("Send application", content)
        self.assertNotIn("Answer required</span>", content)

    def test_queue_renders_select_for_option_bearing_needs_review_field(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "What college did you attend?": {
                    "value": "", "needs_review": True, "required": True,
                    "category": "other", "reason": "invalid_option",
                    "field_type": "single_select",
                    "options": ["State University A", "State University B", "Other"],
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        self.assertIn("<select", content)
        self.assertIn('<option value="State University A"', content)
        self.assertIn('<option value="Other"', content)
        self.assertNotIn('name="value__0" value=""', content)

    def test_queue_select_preselects_an_already_valid_stored_value(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "What college did you attend?": {
                    "value": "State University B", "needs_review": True, "required": True,
                    "category": "other", "reason": "low_confidence",
                    "field_type": "single_select",
                    "options": ["State University A", "State University B", "Other"],
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        self.assertIn('<option value="State University B" selected>', content)
        self.assertNotIn('<option value="State University A" selected>', content)
        self.assertNotIn('<option value="Other" selected>', content)

    def test_queue_renders_text_input_for_free_text_needs_review_field(self):
        job = self._job()
        self._draft(
            self.alice,
            job,
            answers={
                "Why us?": {
                    "value": "", "needs_review": True, "required": False,
                    "category": "other", "reason": "insufficient_evidence",
                    "field_type": "text",
                    "options": [],
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        self.assertNotIn("<select", content)
        self.assertIn('type="text"', content)

    def test_queue_flags_blocking_required_field_in_context(self):
        job = self._job()
        draft = self._draft(
            self.alice,
            job,
            answers={
                "Email": {
                    "value": "a@x.com", "needs_review": False, "required": True,
                    "category": "standard", "reason": "profile",
                },
                "Work authorization?": {
                    "value": "", "needs_review": True, "required": True,
                    "category": "work_authorization", "reason": "hard_excluded_category",
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        rendered_draft = next(d for d in response.context["page_obj"] if d.pk == draft.pk)
        self.assertEqual(rendered_draft.blocking_fields, ["Work authorization?"])

    def test_queue_no_blocking_fields_when_only_optional_gaps_remain(self):
        job = self._job()
        draft = self._draft(
            self.alice,
            job,
            answers={
                "Nickname?": {
                    "value": "", "needs_review": True, "required": False,
                    "category": "other", "reason": "insufficient_evidence",
                },
            },
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        rendered_draft = next(d for d in response.context["page_obj"] if d.pk == draft.pk)
        self.assertEqual(rendered_draft.blocking_fields, [])

    def test_queue_shows_exclusion_reason_as_friendly_message(self):
        job = self._job()
        self._draft(
            self.alice, job, status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason="Required question(s) could not be answered: Visa status",
            reason_code=AutoApplyDraft.ReasonCode.UNANSWERABLE_REQUIRED,
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()

        # Raw internal text is not shown verbatim...
        self.assertNotIn("Visa status", content)
        # ...but a friendly, mapped message is.
        self.assertIn("don&#x27;t have an answer", content)

    def test_queue_shows_generic_message_for_unmapped_failure_text(self):
        job = self._job()
        self._draft(
            self.alice, job, status=AutoApplyDraft.Status.FAILED,
            error_message="Some totally novel internal traceback detail",
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        content = response.content.decode()
        self.assertNotIn("traceback", content)
        self.assertIn("couldn&#x27;t be completed automatically", content)

    def test_queue_only_shows_requesting_users_drafts(self):
        job = self._job()
        self._draft(self.bob, job, status=AutoApplyDraft.Status.DRAFTED)
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        self.assertEqual(len(response.context["page_obj"]), 0)

    def test_queue_reflects_applied_status_after_successful_send(self):
        job = self._job()
        job_application = JobApplication.objects.create(
            user=self.alice, job=job, status=JobApplication.Status.APPLIED
        )
        self._draft(
            self.alice, job, status=AutoApplyDraft.Status.APPLIED,
            job_application=job_application,
        )
        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))
        self.assertContains(response, "Applied")

    def test_status_filter_returns_only_matching_status(self):
        job1, job2, job3 = self._job(), self._job(), self._job()
        self._draft(self.alice, job1, status=AutoApplyDraft.Status.FAILED)
        self._draft(self.alice, job2, status=AutoApplyDraft.Status.DRAFTED)
        self._draft(self.alice, job3, status=AutoApplyDraft.Status.APPLIED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"status": "failed"})

        drafts = list(response.context["page_obj"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].status, AutoApplyDraft.Status.FAILED)

    def test_status_filter_all_shows_all_statuses_by_default(self):
        job1, job2, job3 = self._job(), self._job(), self._job()
        self._draft(self.alice, job1, status=AutoApplyDraft.Status.DRAFTED)
        self._draft(self.alice, job2, status=AutoApplyDraft.Status.EXCLUDED,
                    exclusion_reason="Required question(s) could not be answered: Visa status")
        self._draft(self.alice, job3, status=AutoApplyDraft.Status.STALE)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertEqual(len(response.context["page_obj"]), 3)

    def test_status_filter_invalid_value_falls_back_to_all(self):
        job1, job2 = self._job(), self._job()
        self._draft(self.alice, job1, status=AutoApplyDraft.Status.DRAFTED)
        self._draft(self.alice, job2, status=AutoApplyDraft.Status.APPLIED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"status": "bogus"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 2)

    def test_status_filter_preserves_param_in_pagination_links(self):
        for _ in range(25):
            self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.FAILED)
        self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.APPLIED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"status": "failed", "page": "1"})

        self.assertContains(response, "status=failed")

    def test_status_filter_empty_result_shows_filtered_empty_message(self):
        self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"status": "applied"})

        self.assertContains(response, "No drafts")
        self.assertContains(response, "Applied")
        self.assertNotContains(response, "No auto-apply drafts yet")

    def test_ats_filter_returns_only_matching_ats(self):
        gh_job = self._job(ats="greenhouse")
        lever_job = self._job(ats="lever")
        self._draft(self.alice, gh_job, status=AutoApplyDraft.Status.DRAFTED)
        self._draft(self.alice, lever_job, status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"ats": "lever"})

        drafts = list(response.context["page_obj"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].job_id, lever_job.id)

    def test_ats_filter_invalid_value_falls_back_to_all(self):
        self._draft(self.alice, self._job(ats="greenhouse"), status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"ats": "bogus"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_status_and_ats_filters_combine(self):
        gh_job, lever_job = self._job(ats="greenhouse"), self._job(ats="lever")
        self._draft(self.alice, gh_job, status=AutoApplyDraft.Status.FAILED)
        self._draft(self.alice, lever_job, status=AutoApplyDraft.Status.FAILED)
        self._draft(self.alice, self._job(ats="greenhouse"), status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"), {"status": "failed", "ats": "lever"})

        drafts = list(response.context["page_obj"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].job_id, lever_job.id)

    def test_collapsible_cards_default_closed_regardless_of_status(self):
        # Every card starts collapsed, including FAILED and a DRAFTED draft
        # with blocking fields -- the user always clicks to expand.
        failed_draft = self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.FAILED)
        blocking_draft = self._draft(
            self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED,
            answers={"Visa status": {"value": "", "required": True, "needs_review": True}},
        )
        job = self._job()
        job_application = JobApplication.objects.create(
            user=self.alice, job=job, status=JobApplication.Status.APPLIED
        )
        self._draft(self.alice, job, status=AutoApplyDraft.Status.APPLIED, job_application=job_application)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertNotIn("<details open", response.content.decode())
        del failed_draft, blocking_draft  # used only to create the rows

    def test_collapsible_summary_shows_blocking_and_review_counts(self):
        self._draft(
            self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED,
            answers={
                "Visa status": {"value": "", "required": True, "needs_review": True},
                "Work permit": {"value": "", "required": True, "needs_review": True},
                "Gender": {"value": "Male", "required": False, "needs_review": True},
            },
        )

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertContains(response, "2 required fields missing")
        self.assertContains(response, "3 needs review")

    def test_repeat_attempts_for_same_job_nest_under_one_card(self):
        # Multiple failed retries for the same job (e.g. while a bug was
        # being fixed) must collapse into one top-level card with the
        # older attempts nested, instead of one redundant card each.
        job = self._job()
        import time as _time

        d1 = self._draft(self.alice, job, status=AutoApplyDraft.Status.FAILED,
                          error_message="first failure")
        _time.sleep(0.01)
        d2 = self._draft(self.alice, job, status=AutoApplyDraft.Status.FAILED,
                          error_message="second failure")
        _time.sleep(0.01)
        d3 = self._draft(self.alice, job, status=AutoApplyDraft.Status.FAILED,
                          error_message="third failure")

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertEqual(len(response.context["job_groups"]), 1)
        group = response.context["job_groups"][0]
        self.assertEqual(group["latest"].pk, d3.pk)
        self.assertEqual([d.pk for d in group["earlier"]], [d2.pk, d1.pk])
        self.assertContains(response, "2 earlier attempts for this job")

    def test_different_jobs_each_get_their_own_card(self):
        self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.FAILED)
        self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED)

        client = self._client_for(self.alice)
        response = client.get(reverse("auto_apply_queue"))

        self.assertEqual(len(response.context["job_groups"]), 2)
        for group in response.context["job_groups"]:
            self.assertEqual(group["earlier"], [])

    def test_discard_redirect_preserves_status_filter(self):
        draft = self._draft(self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED)
        client = self._client_for(self.alice)

        response = client.post(
            reverse("discard_auto_apply_draft", args=[draft.id]),
            {"status": "drafted", "ats": "greenhouse"},
        )

        self.assertIn("status=drafted", response.url)
        self.assertIn("ats=greenhouse", response.url)

    def test_send_redirect_preserves_status_filter(self):
        draft = self._draft(
            self.alice, self._job(), status=AutoApplyDraft.Status.DRAFTED,
            answers={"Visa status": {"value": "", "required": True, "needs_review": True}},
        )
        client = self._client_for(self.alice)

        response = client.post(
            reverse("send_auto_apply_draft", args=[draft.id]),
            {"status": "drafted"},
        )

        self.assertIn("status=drafted", response.url)

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_retry_redirect_preserves_status_filter(self, mock_task):
        job = self._job()
        self._draft(self.alice, job, status=AutoApplyDraft.Status.FAILED)
        client = self._client_for(self.alice)

        response = client.post(
            reverse("trigger_auto_apply", args=[job.id]),
            {"next": "queue", "status": "failed"},
        )

        self.assertIn("status=failed", response.url)


class EditAutoApplyDraftTests(AutoApplyViewsTestCase):
    def test_edit_validates_choices_and_leaves_blank_answers_unconfirmed(self):
        for value, options, accepted, needs_review in (
            ("Forged", ["Yes", "No"], False, True),
            ("Yes", ["Yes", "No"], True, False),
            ("", ["Yes", "No"], True, True),
            ("  ", [], True, True),
        ):
            with self.subTest(value=value):
                draft = self._draft(self.alice, self._job(), answers={
                    "Q": {"value": "No", "options": options, "needs_review": True},
                    "Text": {"value": "original", "needs_review": True},
                })
                response = self._client_for(self.alice).post(
                    reverse("edit_auto_apply_draft", args=[draft.id]),
                    {"label__0": "Text", "value__0": "changed", "label__1": "Q", "value__1": value},
                    follow=True,
                )
                draft.refresh_from_db()
                self.assertEqual(draft.answers["Q"]["value"], value if accepted else "No")
                self.assertEqual(draft.answers["Q"]["needs_review"], needs_review)
                # A rejected "Q" doesn't discard the valid sibling "Text"
                # edit from the same POST (see edit_auto_apply_draft's
                # docstring) -- Text always saves regardless of Q's outcome.
                self.assertEqual(draft.answers["Text"]["value"], "changed")
                if not accepted:
                    self.assertContains(response, "Choose a valid answer for Q.")

    def test_edit_updates_answers_and_clears_needs_review(self):
        job = self._job()
        draft = self._draft(
            self.alice, job,
            answers={
                "Why us?": {"value": "old answer", "needs_review": True, "category": "other", "reason": "x"},
            },
        )
        client = self._client_for(self.alice)
        response = client.post(
            reverse("edit_auto_apply_draft", args=[draft.id]),
            {"label__0": "Why us?", "value__0": "new confirmed answer"},
        )
        self.assertRedirects(response, reverse("auto_apply_queue"))
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Why us?"]["value"], "new confirmed answer")
        self.assertFalse(draft.answers["Why us?"]["needs_review"])

    def test_edit_rejects_non_drafted_status(self):
        job = self._job()
        draft = self._draft(
            self.alice, job, status=AutoApplyDraft.Status.EXCLUDED,
            answers={"Q": {"value": "v", "needs_review": True, "category": "x", "reason": "x"}},
        )
        client = self._client_for(self.alice)
        response = client.post(
            reverse("edit_auto_apply_draft", args=[draft.id]),
            {"label__0": "Q", "value__0": "hacked"},
        )
        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Q"]["value"], "v")
        self.assertTrue(draft.answers["Q"]["needs_review"])

    def test_edit_another_users_draft_404s_and_leaves_it_unchanged(self):
        """Critical security regression test: cross-user edit must 404, not
        mutate another user's draft (the IDOR the plan review caught)."""
        job = self._job()
        draft = self._draft(
            self.bob, job,
            answers={"Q": {"value": "bob's original", "needs_review": True, "category": "x", "reason": "x"}},
        )
        client = self._client_for(self.alice)
        response = client.post(
            reverse("edit_auto_apply_draft", args=[draft.id]),
            {"label__0": "Q", "value__0": "alice was here"},
        )
        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(draft.answers["Q"]["value"], "bob's original")
        self.assertTrue(draft.answers["Q"]["needs_review"])


class SendAutoApplyDraftTests(AutoApplyViewsTestCase):
    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_transitions_drafted_to_sending_and_enqueues_task(self, mock_task):
        job = self._job()
        draft = self._draft(self.alice, job)
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]), follow=True)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        mock_task.delay.assert_called_once_with(draft.id)
        self.assertRedirects(response, reverse("auto_apply_queue"))

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_bumps_updated_at_so_the_sweep_leaves_a_fresh_send_alone(self, mock_task):
        # `.update()` skips auto_now: a draft that sat in the queue before Send
        # kept its old updated_at and the 5-minute sweep then recovered the
        # in-flight send as "stuck".
        from datetime import timedelta

        from django.utils import timezone

        from apps.auto_apply.tasks import sweep_stale_auto_apply_drafts

        job = self._job()
        draft = self._draft(self.alice, job)
        AutoApplyDraft.objects.filter(pk=draft.pk).update(
            updated_at=timezone.now() - timedelta(minutes=10)
        )
        client = self._client_for(self.alice)

        client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        sweep_stale_auto_apply_drafts()

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_blocked_when_required_field_still_blank(self, mock_task):
        job = self._job()
        draft = self._draft(
            self.alice,
            job,
            answers={
                "Work authorization?": {
                    "value": "", "needs_review": True, "required": True,
                    "category": "work_authorization", "reason": "hard_excluded_category",
                },
            },
        )
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]), follow=True)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        mock_task.delay.assert_not_called()
        self.assertContains(response, "Work authorization?")

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_succeeds_when_only_optional_field_unanswered(self, mock_task):
        job = self._job()
        draft = self._draft(
            self.alice,
            job,
            answers={
                "Nickname?": {
                    "value": "", "needs_review": True, "required": False,
                    "category": "other", "reason": "insufficient_evidence",
                },
            },
        )
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]), follow=True)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        mock_task.delay.assert_called_once_with(draft.id)
        self.assertRedirects(response, reverse("auto_apply_queue"))

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_blocked_when_required_field_is_whitespace_only(self, mock_task):
        """Regression guard: a whitespace-only value must not count as a
        real answer -- `edit_auto_apply_draft` performs no blank-input
        validation, so without `.strip()` in `_blocking_required_fields`, a
        single-space submission would "answer" a hard-excluded-category
        question (work authorization, salary, legal attestation) and let it
        reach the employer with no real content."""
        job = self._job()
        draft = self._draft(
            self.alice,
            job,
            answers={
                "Work authorization?": {
                    "value": "  ", "needs_review": True, "required": True,
                    "category": "work_authorization", "reason": "hard_excluded_category",
                },
            },
        )
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]), follow=True)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        mock_task.delay.assert_not_called()
        self.assertContains(response, "Work authorization?")

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_rejects_stale_draft(self, mock_task):
        job = self._job()
        draft = self._draft(self.alice, job, status=AutoApplyDraft.Status.STALE)
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]))

        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.STALE)
        mock_task.delay.assert_not_called()

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_rejects_excluded_draft(self, mock_task):
        job = self._job()
        draft = self._draft(self.alice, job, status=AutoApplyDraft.Status.EXCLUDED)
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]))

        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        mock_task.delay.assert_not_called()

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_double_post_send_results_in_one_transition_and_one_enqueue(self, mock_task):
        job = self._job()
        draft = self._draft(self.alice, job)
        client = self._client_for(self.alice)

        response1 = client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        response2 = client.post(reverse("send_auto_apply_draft", args=[draft.id]))

        self.assertEqual(response1.status_code, 302)
        self.assertEqual(response2.status_code, 404)
        mock_task.delay.assert_called_once_with(draft.id)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_another_users_draft_404s_and_status_unchanged(self, mock_task):
        """Critical security regression test: cross-user send must 404, not
        flip another user's draft to SENDING (the IDOR the plan review
        caught in an earlier version of this unit)."""
        job = self._job()
        draft = self._draft(self.bob, job, status=AutoApplyDraft.Status.DRAFTED)
        client = self._client_for(self.alice)

        response = client.post(reverse("send_auto_apply_draft", args=[draft.id]))

        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        mock_task.delay.assert_not_called()

    def test_send_requires_login(self):
        job = self._job()
        draft = self._draft(self.alice, job)
        response = Client().post(reverse("send_auto_apply_draft", args=[draft.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class DraftReviewRegressionTests(AutoApplyViewsTestCase):
    def _custom_draft(self, field_type=TEXT, *, required=True, options=(), options_complete=True):
        return draft_for(
            self.alice, self._job(),
            form_client=FakeFormClient(FormSchema(fields=(
                FormField(
                    "Custom question", field_type, required,
                    options=options, options_complete=options_complete,
                ),
            ))),
            llm_client=FakeLLMClient(),
        )

    def test_required_fallback_preserves_explicit_answer_flags(self):
        snapshot = schema_to_dict(FormSchema(fields=(FormField("Q", TEXT, True),)))
        for entry, expected in (
            ({"value": ""}, ["Q"]),
            ({"value": "  "}, ["Q"]),
            ({"value": "answered"}, []),
            ({"value": "", "required": False}, []),
            ({"value": "", "required": True}, ["Q"]),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(_blocking_required_fields({"Q": entry}, snapshot), expected)
        self.assertEqual(_blocking_required_fields({"Q": {"value": ""}}), [])
        snapshot["fields"][0]["required"] = False
        self.assertEqual(
            _blocking_required_fields({"Q": {"value": "", "required": True}}, snapshot), ["Q"]
        )

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_legacy_blank_answer_blocks_queue_and_send(self, task):
        draft = self._custom_draft()
        del draft.answers["Custom question"]["required"]
        draft.save(update_fields=["answers"])
        client = self._client_for(self.alice)
        queue = client.get(reverse("auto_apply_queue"))
        self.assertContains(queue, "Answer required</span>")
        self.assertNotContains(queue, "Send application")
        client.post(reverse("send_auto_apply_draft", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        task.delay.assert_not_called()

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_edit_required_placeholder_then_send_same_draft(self, task):
        draft = self._custom_draft()
        client = self._client_for(self.alice)
        response = client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
            "label__0": "Custom question", "value__0": "My answer",
        })
        self.assertRedirects(response, reverse("auto_apply_queue"))
        client.post(reverse("send_auto_apply_draft", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Custom question"]["value"], "My answer")
        self.assertFalse(draft.answers["Custom question"]["needs_review"])
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        task.delay.assert_called_once_with(draft.pk)

    def test_option_controls_render_as_select_with_multiple_where_expected(self):
        client = self._client_for(self.alice)
        for field_type in (SINGLE_SELECT, COMBOBOX_SELECT, MULTI_SELECT, CHECKBOX_GROUP):
            with self.subTest(field_type=field_type):
                draft = self._custom_draft(field_type, options=("Yes", "No"))
                queue = client.get(reverse("auto_apply_queue"))
                self.assertContains(queue, '<select name="value__0"')
                self.assertContains(queue, '<option value="Yes">Yes</option>', html=True)
                if field_type in (MULTI_SELECT, CHECKBOX_GROUP):
                    self.assertContains(queue, "multiple")
                draft.delete()

    def test_option_controls_reject_a_value_outside_the_option_set(self):
        client = self._client_for(self.alice)
        for field_type in (SINGLE_SELECT, COMBOBOX_SELECT, MULTI_SELECT, CHECKBOX_GROUP):
            with self.subTest(field_type=field_type):
                draft = self._custom_draft(field_type, options=("Yes", "No"))
                draft.refresh_from_db()  # tuples (in-memory) -> lists (JSONField) before comparing
                original = draft.answers
                response = client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
                    "label__0": "Custom question", "value__0": "Not an option",
                }, follow=True)
                self.assertContains(response, "Choose a valid answer for Custom question")
                draft.refresh_from_db()
                self.assertEqual(draft.answers, original)
                draft.delete()

    def test_option_controls_round_trip_a_valid_selected_value(self):
        client = self._client_for(self.alice)
        for field_type in (SINGLE_SELECT, COMBOBOX_SELECT, MULTI_SELECT, CHECKBOX_GROUP):
            with self.subTest(field_type=field_type):
                draft = self._custom_draft(field_type, options=("Yes", "No"))
                multiple = field_type in (MULTI_SELECT, CHECKBOX_GROUP)
                value = ["Yes", "No"] if multiple else "Yes"
                client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
                    "label__0": "Custom question", "value__0": value,
                })
                draft.refresh_from_db()
                self.assertEqual(draft.answers["Custom question"]["value"], value)
                self.assertFalse(draft.answers["Custom question"]["needs_review"])
                queue = client.get(reverse("auto_apply_queue"))
                self.assertContains(queue, '<option value="Yes" selected>Yes</option>', html=True)
                if multiple:
                    self.assertContains(queue, '<option value="No" selected>No</option>', html=True)
                draft.delete()

    def test_incomplete_combobox_options_permit_a_value_outside_the_sample(self):
        # A COMBOBOX_SELECT's DOM-sampled options can be a partial list
        # (options_complete=False) -- the live combobox search could still
        # find a value that isn't in that sample, so editing must not
        # enforce it as a closed allowlist (see views._answer_edit_field).
        draft = self._custom_draft(
            COMBOBOX_SELECT, options=("Sample A", "Sample B"), options_complete=False,
        )
        client = self._client_for(self.alice)
        response = client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
            "label__0": "Custom question", "value__0": "Not in the sample",
        }, follow=True)
        self.assertNotContains(response, "Choose a valid answer for Custom question")
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Custom question"]["value"], "Not in the sample")

    def test_incomplete_combobox_options_render_as_suggestions(self):
        # An incomplete COMBOBOX_SELECT sample must still surface its known
        # options (e.g. a Yes/No employer question) as clickable suggestions
        # rather than a blank text box the user has to guess into.
        draft = self._custom_draft(
            COMBOBOX_SELECT, options=("Yes", "No"), options_complete=False,
        )
        client = self._client_for(self.alice)
        queue = client.get(reverse("auto_apply_queue"))
        self.assertContains(queue, "<datalist")
        self.assertContains(queue, '<option value="Yes">', html=False)
        self.assertContains(queue, '<option value="No">', html=False)
        draft.delete()

    def test_edit_survives_a_label_with_an_embedded_newline(self):
        # An employer-supplied question can legitimately span multiple
        # lines. The queue template puts the label into a hidden <input>'s
        # `value` attribute; per the HTML form-data-set algorithm, browsers
        # normalize a lone "\n" there to "\r\n" on submit. `answers` is
        # keyed with the original bare "\n", so the edit view must undo that
        # normalization before looking the label up -- otherwise the field
        # (and whatever the user typed) is silently dropped.
        label = "Are you employed by our group,\nincluding its subsidiaries?"
        draft = draft_for(
            self.alice, self._job(),
            form_client=FakeFormClient(FormSchema(fields=(
                FormField(label, COMBOBOX_SELECT, True, options=("Yes", "No"), options_complete=False),
            ))),
            llm_client=FakeLLMClient(),
        )
        client = self._client_for(self.alice)
        client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
            "label__0": label.replace("\n", "\r\n"),
            "value__0": "No",
        })
        draft.refresh_from_db()
        self.assertEqual(draft.answers[label]["value"], "No")
        self.assertFalse(draft.answers[label]["needs_review"])

    def test_option_labels_render_html_escaped(self):
        # Defense-in-depth regression test: Django's widget rendering already
        # escapes option labels, but an option label originating from
        # employer-supplied form data reaching the page unescaped would be
        # stored XSS, so lock in that it stays escaped.
        draft = self._custom_draft(SINGLE_SELECT, options=("<script>alert(1)</script>", "No"))
        client = self._client_for(self.alice)
        queue = client.get(reverse("auto_apply_queue"))
        self.assertNotContains(queue, "<script>alert(1)</script>")
        self.assertContains(queue, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_empty_multiple_choice_remains_blocking(self):
        draft = self._custom_draft(MULTI_SELECT, options=("Yes", "No"))
        client = self._client_for(self.alice)
        # Browsers omit an unselected multiple select from POST entirely.
        client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
            "label__0": "Custom question",
        })
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Custom question"]["value"], [])
        self.assertTrue(draft.answers["Custom question"]["needs_review"])
        self.assertEqual(_blocking_required_fields(draft.answers), ["Custom question"])

    def test_combobox_without_snapshot_options_accepts_typed_answer(self):
        draft = self._custom_draft(COMBOBOX_SELECT)
        client = self._client_for(self.alice)
        client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
            "label__0": "Custom question", "value__0": "London",
        })
        draft.refresh_from_db()
        self.assertEqual(draft.answers["Custom question"]["value"], "London")
        self.assertFalse(draft.answers["Custom question"]["needs_review"])

    def test_file_answers_stay_read_only_even_with_legacy_metadata(self):
        import copy

        draft = self._custom_draft(FILE, required=False)
        client = self._client_for(self.alice)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    del draft.answers["Custom question"]["field_type"]
                    draft.save(update_fields=["answers"])
                original = copy.deepcopy(draft.answers)
                queue = client.get(reverse("auto_apply_queue"))
                self.assertNotContains(queue, 'name="value__0"')
                client.post(reverse("edit_auto_apply_draft", args=[draft.pk]), {
                    "label__0": "Custom question", "value__0": "/etc/passwd",
                })
                draft.refresh_from_db()
                self.assertEqual(draft.answers, original)
                if legacy:
                    self.assertNotIn("field_type", draft.answers["Custom question"])

    def test_discard_blocked_draft_allows_fresh_draft_for_same_job(self):
        draft = self._custom_draft()
        client = self._client_for(self.alice)
        queue = client.get(reverse("auto_apply_queue"))
        self.assertContains(queue, reverse("discard_auto_apply_draft", args=[draft.pk]))
        response = client.post(reverse("discard_auto_apply_draft", args=[draft.pk]))
        self.assertRedirects(response, reverse("auto_apply_queue"))
        self.assertFalse(AutoApplyDraft.objects.filter(pk=draft.pk).exists())
        fresh = draft_for(
            self.alice, draft.job,
            form_client=FakeFormClient(FormSchema(fields=(FormField("Q", TEXT, True),))),
            llm_client=FakeLLMClient(),
        )
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh.pk, draft.pk)
        self.assertEqual(fresh.status, AutoApplyDraft.Status.DRAFTED)

    def test_discard_requires_owner_login_post_and_drafted_status(self):
        draft = self._custom_draft()
        url = reverse("discard_auto_apply_draft", args=[draft.pk])
        self.assertEqual(Client().post(url).status_code, 302)
        self.assertEqual(self._client_for(self.bob).post(url).status_code, 404)
        client = self._client_for(self.alice)
        self.assertEqual(client.get(url).status_code, 405)
        for status in AutoApplyDraft.Status.values:
            if status == AutoApplyDraft.Status.DRAFTED:
                continue
            with self.subTest(status=status):
                draft.status = status
                draft.save(update_fields=["status"])
                self.assertEqual(client.post(url).status_code, 404)
                self.assertTrue(AutoApplyDraft.objects.filter(pk=draft.pk, status=status).exists())


class NewVerificationReasonCodeMessageTests(AutoApplyQueueViewTests):

    def test_new_reason_codes_render_friendly_messages_and_links(self):
        job = self._job()
        client = self._client_for(self.alice)

        test_cases = [
            (
                AutoApplyDraft.ReasonCode.NO_INBOX_CREDENTIALS,
                "This employer requires email verification. Connect an inbox credential in your settings.",
                True,
            ),
            (
                AutoApplyDraft.ReasonCode.VERIFICATION_CODE_TIMEOUT,
                "Verification email did not arrive in time. Please try sending again.",
                False,
            ),
            (
                AutoApplyDraft.ReasonCode.INBOX_AUTH_FAILED,
                "Could not log into your connected inbox (App Password may be revoked). Please update your credentials in settings.",
                True,
            ),
            (
                AutoApplyDraft.ReasonCode.INBOX_UNAVAILABLE,
                "Could not reach your email provider. Please try sending again.",
                False,
            ),
            (
                AutoApplyDraft.ReasonCode.VERIFICATION_CODE_AMBIGUOUS,
                "Multiple verification emails arrived. Please try sending again.",
                False,
            ),
            (
                AutoApplyDraft.ReasonCode.VERIFICATION_CODE_REJECTED,
                "Verification code was rejected by the employer. Please try sending again.",
                False,
            ),
        ]

        for code, expected_msg, expects_settings_link in test_cases:
            AutoApplyDraft.objects.all().delete()
            draft = self._draft(
                self.alice,
                job,
                status=AutoApplyDraft.Status.FAILED,
                reason_code=code,
            )
            response = client.get(reverse("auto_apply_queue"))
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            self.assertIn(expected_msg, content)
            if expects_settings_link:
                self.assertIn(reverse("email_inbox_credential"), content)


class ReviewFindingViewTests(AutoApplyViewsTestCase):
    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_rejects_unresolved_answers(self, task):
        draft = self._draft(self.alice, self._job(), answers={"Q": {"value": "guess", "needs_review": True}})
        response = self._client_for(self.alice).post(reverse("send_auto_apply_draft", args=[draft.pk]), follow=True)
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        task.delay.assert_not_called()
        self.assertContains(response, "Please review and save")

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_resets_timeout_start(self, task):
        from datetime import timedelta
        from django.utils import timezone
        draft = self._draft(self.alice, self._job())
        AutoApplyDraft.objects.filter(pk=draft.pk).update(updated_at=timezone.now() - timedelta(days=1))
        before = timezone.now()
        self._client_for(self.alice).post(reverse("send_auto_apply_draft", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertGreaterEqual(draft.updated_at, before)
        task.delay.assert_called_once_with(draft.pk)

    def test_queue_file_entry_hides_storage_key_and_omits_value_input(self):
        self._draft(self.alice, self._job(), answers={"Resume": {"value": "resumes/private/key.pdf", "field_type": "file"}, "Email": {"value": "alice@example.com", "field_type": "text"}})
        response = self._client_for(self.alice).get(reverse("auto_apply_queue"))
        self.assertNotContains(response, "resumes/private/key.pdf")
        entries = list(response.context["page_obj"].object_list[0].answers)
        self.assertNotContains(response, f'name="value__{entries.index("Resume")}"')
        self.assertContains(response, f'name="value__{entries.index("Email")}"')
        self.assertContains(response, "Resume attached")

    @mock.patch("apps.web.views.draft_auto_apply")
    def test_unconfirmed_submission_cannot_be_retriggered(self, task):
        job = self._job()
        self._draft(self.alice, job, status=AutoApplyDraft.Status.FAILED, reason_code=AutoApplyDraft.ReasonCode.SUBMISSION_UNCONFIRMED)
        self._client_for(self.alice).post(reverse("trigger_auto_apply", args=[job.pk]))
        task.delay.assert_not_called()


UNCONFIRMED_AUTH_ANSWERS = {
    "Work authorization?": {
        "value": "Yes", "needs_review": True, "needs_confirmation": True,
        "required": True, "category": "work_authorization", "reason": "answer_bank",
        "tier": "t0_legal",
        "provenance": {"origin": "answer_bank", "source": "learned", "locked": False},
    },
}


class UnconfirmedAnswerGateTests(AutoApplyViewsTestCase):
    """A learned/imported T0/T1 answer is held until the user explicitly
    confirms it (Profile Overhaul Phase 1)."""

    def _unconfirmed_draft(self):
        import copy

        return self._draft(
            self.alice, self._job(), answers=copy.deepcopy(UNCONFIRMED_AUTH_ANSWERS)
        )

    def _edit(self, draft, **post):
        data = {"label__0": "Work authorization?", "value__0": "Yes"}
        data.update(post)
        return self._client_for(self.alice).post(
            reverse("edit_auto_apply_draft", args=[draft.id]), data
        )

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_send_is_refused_while_an_answer_is_unconfirmed(self, mock_task):
        draft = self._unconfirmed_draft()

        response = self._client_for(self.alice).post(
            reverse("send_auto_apply_draft", args=[draft.id]), follow=True
        )

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertIsNone(draft.submitted_answers_snapshot)
        mock_task.delay.assert_not_called()
        self.assertContains(response, "Confirm these answer(s) before sending")

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_plain_save_does_not_confirm_and_send_stays_refused(self, mock_task):
        draft = self._unconfirmed_draft()

        self._edit(draft)  # saves the same value, no confirm checkbox

        draft.refresh_from_db()
        entry = draft.answers["Work authorization?"]
        self.assertTrue(entry["needs_confirmation"])
        self.assertTrue(entry["needs_review"])
        self.assertFalse(entry["user_confirmed"])
        self._client_for(self.alice).post(reverse("send_auto_apply_draft", args=[draft.id]))
        mock_task.delay.assert_not_called()

    def test_editing_the_value_without_the_checkbox_is_still_unconfirmed(self):
        draft = self._unconfirmed_draft()

        self._edit(draft, value__0="No")

        draft.refresh_from_db()
        entry = draft.answers["Work authorization?"]
        self.assertEqual(entry["value"], "No")
        self.assertTrue(entry["needs_confirmation"])

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_explicit_confirm_then_send_succeeds_and_records_the_snapshot(self, mock_task):
        draft = self._unconfirmed_draft()

        self._edit(draft, confirm__0="on")

        draft.refresh_from_db()
        entry = draft.answers["Work authorization?"]
        self.assertFalse(entry["needs_confirmation"])
        self.assertFalse(entry["needs_review"])
        self.assertTrue(entry["user_confirmed"])
        self.assertEqual(entry["provenance"]["source"], "user")
        self.assertEqual(entry["confirmed_from"]["source"], "learned")

        self._client_for(self.alice).post(reverse("send_auto_apply_draft", args=[draft.id]))

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        mock_task.delay.assert_called_once_with(draft.id)
        recorded = draft.submitted_answers_snapshot["answers"]["Work authorization?"]
        self.assertEqual(recorded["value"], "Yes")
        self.assertEqual(recorded["tier"], "t0_legal")
        self.assertEqual(recorded["provenance"]["source"], "user")
        self.assertEqual(recorded["confirmed_from"]["source"], "learned")

    def test_confirm_checkbox_on_a_blank_value_does_not_confirm(self):
        draft = self._unconfirmed_draft()

        self._edit(draft, value__0="", confirm__0="on")

        draft.refresh_from_db()
        self.assertTrue(draft.answers["Work authorization?"]["needs_confirmation"])

    def test_queue_shows_the_confirm_control_for_an_unconfirmed_answer(self):
        self._unconfirmed_draft()

        response = self._client_for(self.alice).get(reverse("auto_apply_queue"))

        self.assertContains(response, "Needs your confirmation")
        self.assertContains(response, 'name="confirm__0"')

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_drafts_without_flagged_answers_send_exactly_as_before(self, mock_task):
        draft = self._draft(
            self.alice, self._job(),
            answers={"Why us?": {"value": "x", "needs_review": False}},
        )

        self._client_for(self.alice).post(reverse("send_auto_apply_draft", args=[draft.id]))

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        self.assertIn("Why us?", draft.submitted_answers_snapshot["answers"])


class LearnedAnswerEndToEndTests(AutoApplyViewsTestCase):
    """Real drafting -> review queue -> send, with no hand-built answers: a
    learned T0 answer must be held until the user confirms it."""

    QUESTION = "Are you legally authorized to work in the United States?"

    @mock.patch("apps.web.views.submit_auto_apply_draft")
    def test_learned_work_authorization_is_held_until_confirmed(self, mock_task):
        from apps.accounts.services.answer_resolver import write_answer
        from apps.auto_apply.greenhouse_form.field_mapping import (
            SINGLE_SELECT, FormField, FormSchema,
        )
        from apps.auto_apply.services.drafting import draft_for
        from apps.auto_apply.tests.test_drafting_service import (
            STANDARD_ONLY_SCHEMA, FakeFormClient, FakeLLMClient,
        )

        self.alice.email = "alice@example.com"
        self.alice.save()
        profile = self.alice.profile
        profile.full_name = "Alice Smith"
        profile.phone = "555-1234"
        profile.linkedin_url = "https://linkedin.com/in/alice"
        profile.resume_text = "Alice Smith. Engineer."
        profile.save()
        write_answer(profile, self.QUESTION, "Yes", "learned", options=("Yes", "No"))
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (FormField(self.QUESTION, SINGLE_SELECT, True, ("Yes", "No")),)
        )
        job = self._job()

        draft = draft_for(
            self.alice, job, form_client=FakeFormClient(schema=schema),
            llm_client=FakeLLMClient(),
        )
        client = self._client_for(self.alice)
        index = list(draft.answers).index(self.QUESTION)

        client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        mock_task.delay.assert_not_called()

        # The review form posts every answer, indexed from 0, like the template.
        posted = {}
        for i, (label, entry) in enumerate(draft.answers.items()):
            posted[f"label__{i}"] = label
            posted[f"value__{i}"] = entry["value"]
        posted[f"confirm__{index}"] = "on"
        client.post(reverse("edit_auto_apply_draft", args=[draft.id]), posted)
        client.post(reverse("send_auto_apply_draft", args=[draft.id]))

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        recorded = draft.submitted_answers_snapshot["answers"][self.QUESTION]
        self.assertEqual(recorded["confirmed_from"]["source"], "learned")
        self.assertEqual(recorded["provenance"]["source"], "user")
