"""Tests for the drafting orchestration service (U6): `draft_for`.

`GreenhouseFormClient` (U3) and the `AnswerInferenceClient` (U4) are both
faked at their public-interface boundary -- no Playwright browser and no
Anthropic call is ever exercised here, only the orchestration logic that
wires U1-U4 together into an `AutoApplyDraft`.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormSchemaMismatch,
)
from apps.auto_apply.greenhouse_form.field_mapping import (
    FormField,
    FormSchema,
    SINGLE_SELECT,
    TEXT,
)
from apps.auto_apply.llm.base import QuestionAnswer
from apps.auto_apply.models import AutoApplyDraft, ExplicitAnswer
from apps.auto_apply.services.drafting import draft_for
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


class FakeFormClient:
    """Test double for `GreenhouseFormClient`. Records every `inspect()`
    call so tests can assert the exact `job.source_url` navigated to."""

    def __init__(self, schema=None, raises=None):
        self.schema = schema
        self.raises = raises
        self.inspect_calls: list[str] = []

    def inspect(self, job_url):
        self.inspect_calls.append(job_url)
        if self.raises is not None:
            raise self.raises
        return self.schema


class FakeLLMClient:
    """Test double satisfying `AnswerInferenceClient`. Returns
    pre-programmed `QuestionAnswer`s keyed by question id and records every
    batch passed to `infer()`."""

    def __init__(self, answers_by_id=None, raises=None):
        self.answers_by_id = answers_by_id or {}
        self.raises = raises
        self.calls = []

    def infer(self, questions, resume_text, profile):
        self.calls.append((questions, resume_text, profile))
        if self.raises is not None:
            raise self.raises
        return [self.answers_by_id[q.id] for q in questions if q.id in self.answers_by_id]


STANDARD_ONLY_SCHEMA = FormSchema(
    fields=(
        FormField(label="First Name", field_type=TEXT, required=True),
        FormField(label="Last Name", field_type=TEXT, required=True),
        FormField(label="Email", field_type=TEXT, required=True),
        FormField(label="Phone", field_type=TEXT, required=False),
        FormField(label="LinkedIn Profile", field_type=TEXT, required=False),
    )
)


class DraftingServiceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="pw", email="alice@example.com"
        )
        self.profile = self.user.profile
        self.profile.full_name = "Alice Smith"
        self.profile.phone = "555-1234"
        self.profile.linkedin_url = "https://linkedin.com/in/alicesmith"
        self.profile.resume_text = "Alice Smith. Senior Engineer with 5 years of Python."
        self.profile.save()

        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = Job.objects.create(
            source_ats="greenhouse",
            source_job_id="1",
            source_url="https://job-boards.greenhouse.io/acme/jobs/1",
            employer=self.employer,
            title="Backend Engineer",
        )


class NonGreenhouseJobTests(DraftingServiceTestCase):
    def test_non_greenhouse_job_raises_value_error(self):
        self.job.source_ats = "lever"
        self.job.save()

        with self.assertRaises(ValueError):
            draft_for(self.user, self.job, form_client=FakeFormClient(), llm_client=FakeLLMClient())


class StandardFieldsOnlyTests(DraftingServiceTestCase):
    def test_standard_fields_only_job_drafts_successfully(self):
        form_client = FakeFormClient(schema=STANDARD_ONLY_SCHEMA)
        llm_client = FakeLLMClient()

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(form_client.inspect_calls, [self.job.source_url])
        self.assertEqual(llm_client.calls, [], "no custom questions -- LLM must never be called")

        self.assertEqual(draft.answers["First Name"]["value"], "Alice")
        self.assertEqual(draft.answers["Last Name"]["value"], "Smith")
        self.assertEqual(draft.answers["Email"]["value"], "alice@example.com")
        self.assertEqual(draft.answers["Phone"]["value"], "555-1234")
        self.assertEqual(
            draft.answers["LinkedIn Profile"]["value"], "https://linkedin.com/in/alicesmith"
        )
        for field_answer in draft.answers.values():
            self.assertFalse(field_answer["needs_review"])


class ExplicitAnswerCoveredTests(DraftingServiceTestCase):
    def test_explicit_answer_covered_question_drafts_without_calling_llm(self):
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.SPONSORSHIP,
            answer_text="No, I do not require sponsorship.",
        )
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Will you now or in the future require visa sponsorship?",
                    field_type=SINGLE_SELECT,
                    required=True,
                    options=("Yes", "No"),
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient()

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(llm_client.calls, [], "explicit answer must short-circuit the LLM call")
        sponsorship_answer = draft.answers[
            "Will you now or in the future require visa sponsorship?"
        ]
        self.assertEqual(sponsorship_answer["value"], "")
        self.assertTrue(sponsorship_answer["needs_review"])
        self.assertEqual(sponsorship_answer["reason"], "explicit_answer")


class LLMInferableQuestionTests(DraftingServiceTestCase):
    def _schema_with_custom_question(self):
        return FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="How many years of Python experience do you have?",
                    field_type=TEXT,
                    required=False,
                ),
            )
        )

    def test_confident_llm_inference_drafts_with_needs_review_false(self):
        schema = self._schema_with_custom_question()
        llm_client = FakeLLMClient(
            answers_by_id={
                "How many years of Python experience do you have?": QuestionAnswer(
                    question_id="How many years of Python experience do you have?",
                    answer="5 years",
                    evidence=["5 years of Python"],
                    self_reported_confidence=0.95,
                    insufficient_evidence=False,
                )
            }
        )
        form_client = FakeFormClient(schema=schema)

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(len(llm_client.calls), 1)
        python_answer = draft.answers["How many years of Python experience do you have?"]
        self.assertEqual(python_answer["value"], "5 years")
        self.assertFalse(python_answer["needs_review"])

    def test_low_confidence_llm_inference_flags_needs_review(self):
        schema = self._schema_with_custom_question()
        llm_client = FakeLLMClient(
            answers_by_id={
                "How many years of Python experience do you have?": QuestionAnswer(
                    question_id="How many years of Python experience do you have?",
                    answer="5 years",
                    evidence=["5 years of Python"],
                    self_reported_confidence=0.1,
                    insufficient_evidence=False,
                )
            }
        )
        form_client = FakeFormClient(schema=schema)

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        python_answer = draft.answers["How many years of Python experience do you have?"]
        self.assertTrue(python_answer["needs_review"])


class RequiredQuestionUnanswerableTests(DraftingServiceTestCase):
    def test_required_question_with_no_explicit_and_no_confident_llm_answer_excludes(self):
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="What are your salary expectations?",
                    field_type=TEXT,
                    required=True,
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient()  # salary is hard-excluded -- never called

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertIsNotNone(draft.exclusion_reason)
        self.assertIn("salary expectations", draft.exclusion_reason)
        self.assertEqual(draft.answers, {})
        self.assertEqual(llm_client.calls, [])


class RequiredQuestionLLMFailureTests(DraftingServiceTestCase):
    """A required custom question the LLM couldn't answer *because the LLM
    call itself failed* (vendor outage, billing lockout, etc.) must not
    silently exclude the whole draft the same way a genuinely unanswerable
    question does -- the user gets a chance to fill it in via the review
    queue (U8) instead. Contrast with `RequiredQuestionUnanswerableTests`
    (hard-excluded category, never reaches the LLM) and
    `NoResumeUploadedTests` (LLM ran fine but had nothing to cite -- a real
    content judgment, not an infra failure)."""

    def _schema_with_required_custom_question(self):
        return FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="How many years of Python experience do you have?",
                    field_type=TEXT,
                    required=True,
                ),
            )
        )

    def test_llm_call_raising_drafts_for_manual_review_instead_of_excluding(self):
        schema = self._schema_with_required_custom_question()
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient(raises=RuntimeError("credit balance too low"))

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["How many years of Python experience do you have?"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["reason"], "llm_call_failed")

    def test_llm_response_missing_this_question_drafts_for_manual_review(self):
        # FakeLLMClient.infer() silently omits any question id absent from
        # answers_by_id -- exercises MISSING_LLM_RESPONSE (partial-response
        # failure) rather than LLM_CALL_FAILED (whole-batch failure).
        schema = self._schema_with_required_custom_question()
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient(answers_by_id={})

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["How many years of Python experience do you have?"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["reason"], "missing_llm_response")


class SchemaMismatchTests(DraftingServiceTestCase):
    def test_inspect_raising_schema_mismatch_produces_excluded_draft(self):
        form_client = FakeFormClient(
            raises=GreenhouseFormSchemaMismatch("Required field 'Date of birth' has unsupported type 'date'.")
        )
        llm_client = FakeLLMClient()

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertIn("unsupported field", draft.exclusion_reason)
        self.assertIn("Date of birth", draft.exclusion_reason)

    def test_inspect_raising_challenged_also_produces_excluded_draft(self):
        form_client = FakeFormClient(raises=GreenhouseFormChallenged("Bot-detection challenge present."))
        llm_client = FakeLLMClient()

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertIn("Could not load the application form", draft.exclusion_reason)


class NoResumeUploadedTests(DraftingServiceTestCase):
    def test_llm_eligible_question_with_no_resume_text_resolves_to_needs_review_not_a_crash(self):
        self.profile.resume_text = ""
        self.profile.save()
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="What is your favorite part of being an engineer?",
                    field_type=TEXT,
                    required=False,
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        # A real LLM client would report insufficient_evidence given no
        # resume/profile text to ground an answer in -- simulated here.
        llm_client = FakeLLMClient(
            answers_by_id={
                "What is your favorite part of being an engineer?": QuestionAnswer(
                    question_id="What is your favorite part of being an engineer?",
                    answer="",
                    evidence=[],
                    self_reported_confidence=0.0,
                    insufficient_evidence=True,
                )
            }
        )

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        # Must not crash, and since the field isn't required, an
        # unanswerable custom question doesn't block drafting outright.
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertNotIn("What is your favorite part of being an engineer?", draft.answers)

    def test_required_llm_eligible_question_with_no_resume_text_excludes_rather_than_crashing(self):
        self.profile.resume_text = ""
        self.profile.save()
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="What is your favorite part of being an engineer?",
                    field_type=TEXT,
                    required=True,
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient(
            answers_by_id={
                "What is your favorite part of being an engineer?": QuestionAnswer(
                    question_id="What is your favorite part of being an engineer?",
                    answer="",
                    evidence=[],
                    self_reported_confidence=0.0,
                    insufficient_evidence=True,
                )
            }
        )

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertIn("What is your favorite part of being an engineer?", draft.exclusion_reason)


class ConcurrentTriggerGuardTests(DraftingServiceTestCase):
    def test_existing_drafted_row_makes_draft_for_a_no_op(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )
        form_client = FakeFormClient(schema=STANDARD_ONLY_SCHEMA)
        llm_client = FakeLLMClient()

        result = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertIsNone(result)
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 1
        )

    def test_existing_sending_row_also_makes_draft_for_a_no_op(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.SENDING
        )
        form_client = FakeFormClient(schema=STANDARD_ONLY_SCHEMA)
        llm_client = FakeLLMClient()

        result = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertIsNone(result)
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 1
        )


class DraftingBoundaryTests(DraftingServiceTestCase):
    def test_custom_files_never_reach_llm(self):
        from apps.auto_apply.greenhouse_form.field_mapping import FILE
        for required in (False, True):
            with self.subTest(required=required):
                AutoApplyDraft.objects.all().delete()
                client = FakeLLMClient()
                draft = draft_for(self.user, self.job, form_client=FakeFormClient(FormSchema(fields=(FormField("Cover letter", FILE, required),))), llm_client=client)
                self.assertEqual(client.calls, [])
                self.assertNotIn("Cover letter", draft.answers)
                self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED if required else AutoApplyDraft.Status.DRAFTED)

    def test_near_miss_labels_are_not_standard(self):
        from apps.auto_apply.services.drafting import _classify_standard_field
        for label in ("First name of reference", "Email consent", "Phone interview availability", "LinkedIn experience", "Resume summary", "Firstname"):
            self.assertIsNone(_classify_standard_field(label), label)
        self.assertEqual(_classify_standard_field("Resume/CV"), "resume")

    def test_resume_value_is_storage_key(self):
        from apps.auto_apply.services.drafting import _standard_field_value
        self.profile.resume = "resumes/123/random.pdf"
        self.assertEqual(_standard_field_value("resume", self.profile, self.user), "resumes/123/random.pdf")

    def test_profile_labels_on_selects_follow_custom_resolution(self):
        client = FakeLLMClient()
        draft = draft_for(self.user, self.job, form_client=FakeFormClient(FormSchema(fields=(FormField("Email", SINGLE_SELECT, True, ("Yes", "No")),))), llm_client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(draft.answers["Email"]["needs_review"])

    def test_invalid_choice_values_need_review(self):
        from unittest.mock import patch
        from apps.auto_apply.greenhouse_form.field_mapping import MULTI_SELECT, CHECKBOX_GROUP
        from apps.auto_apply.llm.base import ResolvedAnswer
        for kind, value in ((SINGLE_SELECT, "Maybe"), (MULTI_SELECT, ["Yes", "Maybe"]), (CHECKBOX_GROUP, ["Maybe"])):
            with self.subTest(kind=kind):
                AutoApplyDraft.objects.all().delete()
                schema = FormSchema(fields=(FormField("Question", kind, True, ("Yes", "No")),))
                with patch("apps.auto_apply.services.drafting.answer_resolution.resolve_field_answers", return_value=[ResolvedAnswer("Question", "generic", value, False, "ok")]):
                    draft = draft_for(self.user, self.job, form_client=FakeFormClient(schema), llm_client=FakeLLMClient())
                self.assertEqual(draft.answers["Question"]["value"], "")
                self.assertTrue(draft.answers["Question"]["needs_review"])

    def test_authorization_and_sponsorship_answers_never_cross_match(self):
        from apps.auto_apply.services.answer_resolution import resolve_field_answers
        from apps.auto_apply.llm.base import Question
        for saved, question in ((ExplicitAnswer.Category.WORK_AUTHORIZATION, "Require sponsorship?"), (ExplicitAnswer.Category.SPONSORSHIP, "Authorized to work?")):
            with self.subTest(saved=saved):
                ExplicitAnswer.objects.all().delete()
                ExplicitAnswer.objects.create(user=self.user, category=saved, answer_text="Yes")
                client = FakeLLMClient()
                answer = resolve_field_answers(self.user, [Question("q", question)], "", self.profile, client)[0]
                self.assertIsNone(answer.answer)
                self.assertTrue(answer.needs_review)
                self.assertEqual(client.calls, [])

    def test_unconfirmed_submission_blocks_redrafting(self):
        AutoApplyDraft.objects.create(user=self.user, job=self.job, status=AutoApplyDraft.Status.FAILED, reason_code=AutoApplyDraft.ReasonCode.SUBMISSION_UNCONFIRMED)
        client = FakeFormClient(STANDARD_ONLY_SCHEMA)
        self.assertIsNone(draft_for(self.user, self.job, form_client=client, llm_client=FakeLLMClient()))
        self.assertEqual(client.inspect_calls, [])
