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


class EditAutoApplyDraftTests(AutoApplyViewsTestCase):
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
    def _custom_draft(self, field_type=TEXT, *, required=True, options=()):
        return draft_for(
            self.alice, self._job(),
            form_client=FakeFormClient(FormSchema(fields=(
                FormField("Custom question", field_type, required, options=options),
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
