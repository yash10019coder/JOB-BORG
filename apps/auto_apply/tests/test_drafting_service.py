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
    CHECKBOX_ACKNOWLEDGEMENT,
    COMBOBOX_SELECT,
    FILE,
    FormField,
    FormSchema,
    SINGLE_SELECT,
    TEXT,
)
from apps.auto_apply.llm.base import QuestionAnswer
from apps.auto_apply.models import AutoApplyDraft, ExplicitAnswer
from apps.accounts.models import AnswerBank
from apps.auto_apply.tests.legacy_seed import seed_explicit_answer
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


class OptionCompletenessTests(DraftingServiceTestCase):
    def test_only_complete_or_small_combobox_options_constrain_questions(self):
        # The LLM is shown the sampled options either way (so it recognizes
        # a constrained-choice question and doesn't answer in free-text
        # prose) -- only *enforcement* against that sample differs. A large
        # incomplete sample is a hint, not a closed set to reject an
        # out-of-sample answer against (a real, dynamically-searched field
        # like School may have thousands more entries than the DOM sample).
        # A *small* sample -- complete or not -- is enforced regardless:
        # verified against production failures where a small, genuinely
        # complete dropdown (a 10-entry Degree list, a 3-entry salary-range
        # list) was flagged incomplete by the scroll heuristic, and the
        # unenforced answer crashed a live submission instead of going to
        # needs_review.
        large_sample = tuple(f"Sample School {i}" for i in range(20))
        for complete, options, expected_enforced in (
            (False, large_sample, False),
            (True, large_sample, True),
            (False, ("Sample School",), True),  # small -- enforced despite incomplete
            (True, ("Sample School",), True),
        ):
            with self.subTest(complete=complete, size=len(options)):
                llm = FakeLLMClient()
                field = FormField("School", COMBOBOX_SELECT, True, options, options_complete=complete)
                draft = draft_for(self.user, self.job, form_client=FakeFormClient(FormSchema((field,))), llm_client=llm)
                question = llm.calls[0][0][0]
                self.assertEqual(question.options, field.options)
                self.assertEqual(question.options_enforced, expected_enforced)
                self.assertEqual(tuple(draft.answers["School"]["options"]), field.options)
                draft.delete()  # Each case must start without an existing draft.

    def test_standard_field_retains_options(self):
        field = FormField("First Name", SINGLE_SELECT, True, ("Alice", "Other"))
        draft = draft_for(self.user, self.job, form_client=FakeFormClient(FormSchema((field,))), llm_client=FakeLLMClient())
        self.assertEqual(tuple(draft.answers["First Name"]["options"]), field.options)


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
        self.assertTrue(draft.answers["First Name"]["required"])
        self.assertFalse(draft.answers["Phone"]["required"])


class NewStandardFieldsTests(DraftingServiceTestCase):
    """GitHub/portfolio URL and current employer, like linkedin_url and
    phone, are filled straight from Profile data (R4) and never reach the
    LLM -- see `_STANDARD_FIELD_PATTERNS`/`_standard_field_value`."""

    def test_github_website_and_current_company_fill_from_profile(self):
        self.profile.github_url = "https://github.com/alicesmith"
        self.profile.portfolio_url = "https://alicesmith.dev"
        self.profile.current_employer = "Acme Corp"
        self.profile.save()

        schema = FormSchema(fields=(
            FormField(label="GitHub", field_type=TEXT, required=False),
            FormField(label="Github/Gitlab Profile URL", field_type=TEXT, required=False),
            FormField(label="Website", field_type=TEXT, required=False),
            FormField(label="Current Company", field_type=TEXT, required=True),
        ))
        llm_client = FakeLLMClient()
        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=llm_client,
        )

        self.assertEqual(llm_client.calls, [], "standard fields must never reach the LLM")
        self.assertEqual(draft.answers["GitHub"]["value"], "https://github.com/alicesmith")
        self.assertEqual(
            draft.answers["Github/Gitlab Profile URL"]["value"], "https://github.com/alicesmith"
        )
        self.assertEqual(draft.answers["Website"]["value"], "https://alicesmith.dev")
        self.assertEqual(draft.answers["Current Company"]["value"], "Acme Corp")
        for field_answer in draft.answers.values():
            self.assertFalse(field_answer["needs_review"])

    def test_blank_new_standard_fields_fall_back_to_normal_optional_handling(self):
        # None of github_url/portfolio_url/current_employer set (blanks are
        # the model default) -- an optional field with no value is simply
        # omitted, same as any other blank optional standard field.
        schema = FormSchema(fields=(
            FormField(label="GitHub", field_type=TEXT, required=False),
        ))
        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        self.assertNotIn("GitHub", draft.answers)

    def test_required_current_company_blank_excludes_draft(self):
        # A blank *required* standard field signals an incomplete Profile
        # (R4/R6) -- same exclude-the-draft behavior as any other blank
        # required standard field (e.g. First Name).
        schema = FormSchema(fields=(
            FormField(label="Current Company", field_type=TEXT, required=True),
        ))
        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertEqual(draft.reason_code, AutoApplyDraft.ReasonCode.UNANSWERABLE_REQUIRED)


class ExplicitAnswerCoveredTests(DraftingServiceTestCase):
    def test_explicit_answer_covered_question_drafts_without_calling_llm(self):
        # "No" (not free text) -- an explicit answer for an option-bearing
        # question is now validated against the field's real options
        # exactly like an LLM answer (see answer_resolution's
        # _enforce_option_constraint), so the fixture must be a real option.
        seed_explicit_answer(
            user=self.user,
            category=ExplicitAnswer.Category.SPONSORSHIP,
            answer_text="No",
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
        self.assertEqual(sponsorship_answer["value"], "No")
        self.assertFalse(sponsorship_answer["needs_review"])
        self.assertEqual(sponsorship_answer["reason"], "explicit_answer")
        self.assertTrue(sponsorship_answer["required"])


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


class QuestionFieldConstraintPropagationTests(DraftingServiceTestCase):
    """U1: `field_type`/`options` from the discovered `FormField` must reach
    the `Question` passed to the LLM client -- otherwise the LLM has no way
    to know a question is option-constrained (see `answer_resolution`'s
    validation, which relies on `Question.options` being populated)."""

    def test_single_select_question_carries_field_type_and_options(self):
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="What is your highest level of education?",
                    field_type=SINGLE_SELECT,
                    required=True,
                    options=("High School", "Bachelor's", "Master's", "Other"),
                ),
            )
        )
        llm_client = FakeLLMClient(
            answers_by_id={
                "What is your highest level of education?": QuestionAnswer(
                    question_id="What is your highest level of education?",
                    answer="Bachelor's",
                    evidence=["Bachelor's"],
                    self_reported_confidence=0.9,
                )
            }
        )
        form_client = FakeFormClient(schema=schema)

        draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(len(llm_client.calls), 1)
        questions, _, _ = llm_client.calls[0]
        education_question = next(
            q for q in questions if q.id == "What is your highest level of education?"
        )
        self.assertEqual(education_question.field_type, SINGLE_SELECT)
        self.assertEqual(
            education_question.options, ("High School", "Bachelor's", "Master's", "Other")
        )

    def test_text_question_has_no_options(self):
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="How many years of Python experience do you have?",
                    field_type=TEXT,
                    required=False,
                ),
            )
        )
        llm_client = FakeLLMClient(
            answers_by_id={
                "How many years of Python experience do you have?": QuestionAnswer(
                    question_id="How many years of Python experience do you have?",
                    answer="5 years",
                    evidence=["5 years of Python"],
                    self_reported_confidence=0.9,
                )
            }
        )
        form_client = FakeFormClient(schema=schema)

        draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        questions, _, _ = llm_client.calls[0]
        python_question = next(
            q for q in questions if q.id == "How many years of Python experience do you have?"
        )
        self.assertEqual(python_question.field_type, TEXT)
        self.assertEqual(python_question.options, ())


class RequiredQuestionUnanswerableTests(DraftingServiceTestCase):
    """A required custom question the LLM can never answer (a hard-excluded
    category, with no ExplicitAnswer on file) no longer excludes the whole
    draft -- it floats up as a blank, needs_review, required placeholder for
    a human to answer via the review queue instead. Contrast with
    `RequiredQuestionLLMFailureTests` (LLM infra failure -- same treatment,
    different `reason`) and `NoResumeUploadedTests` (LLM ran fine but had
    nothing to cite)."""

    def test_required_hard_excluded_question_with_no_explicit_answer_drafts_for_manual_review(self):
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

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["What are your salary expectations?"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertTrue(entry["required"])
        self.assertEqual(entry["reason"], "hard_excluded_category")
        self.assertEqual(llm_client.calls, [])


    def test_required_standalone_checkbox_attestation_drafts_for_manual_review(self):
        # A required CHECKBOX_ACKNOWLEDGEMENT field labeled with existing
        # LEGAL_ATTESTATION phrasing (see apps/auto_apply/llm/categories.py)
        # is not a FILE-type field, so it does not hit the one
        # exclude-the-draft exception -- it floats up as a blank
        # needs_review placeholder, same as any other hard-excluded
        # required custom question, never auto-checked by the LLM.
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="I agree to the Terms of Service",
                    field_type=CHECKBOX_ACKNOWLEDGEMENT,
                    required=True,
                    options=("Yes", "No"),
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient()  # attestation is hard-excluded -- never called

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["I agree to the Terms of Service"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertTrue(entry["required"])
        self.assertEqual(entry["reason"], "hard_excluded_category")
        self.assertEqual(llm_client.calls, [])


class CarryForwardConfirmedAnswerTests(DraftingServiceTestCase):
    """A hard-excluded (e.g. DEMOGRAPHIC) question the user already
    answered by hand on a prior FAILED/EXCLUDED draft for this exact job --
    via `edit_auto_apply_draft`, which marks the entry `user_confirmed` --
    must not come back blank on a fresh draft (e.g. a Retry) for the same
    (user, job). See `_carry_forward_confirmed_answers`.

    `user_confirmed` (not `needs_review=False`) is the carry-forward
    signal: a confident LLM answer also gets `needs_review=False` with no
    human involved (see `test_unconfirmed_prior_answer_is_not_reused`'s
    sibling below), so `needs_review` alone can't prove the user actually
    looked at it."""

    def _gender_schema(self):
        return FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Gender",
                    field_type=SINGLE_SELECT,
                    required=False,
                    options=("Male", "Female", "Decline To Self Identify"),
                    options_complete=True,
                ),
            )
        )

    def test_confirmed_answer_from_prior_draft_is_reused(self):
        schema = self._gender_schema()
        first_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        self.assertEqual(first_draft.answers["Gender"]["value"], "")
        self.assertTrue(first_draft.answers["Gender"]["needs_review"])

        # Simulate the user confirming the answer via edit_auto_apply_draft,
        # then the draft later failing at send time.
        first_draft.answers["Gender"]["value"] = "Male"
        first_draft.answers["Gender"]["needs_review"] = False
        first_draft.answers["Gender"]["user_confirmed"] = True
        first_draft.status = AutoApplyDraft.Status.FAILED
        first_draft.reason_code = AutoApplyDraft.ReasonCode.SUBMISSION_FAILED
        first_draft.save()

        retry_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )

        entry = retry_draft.answers["Gender"]
        self.assertEqual(entry["value"], "Male")
        self.assertFalse(entry["needs_review"])
        self.assertEqual(entry["reason"], "carried_forward_from_previous_draft")

    def test_confirmed_answer_not_reused_when_no_longer_a_valid_option(self):
        schema = self._gender_schema()
        first_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        first_draft.answers["Gender"]["value"] = "Male"
        first_draft.answers["Gender"]["needs_review"] = False
        first_draft.answers["Gender"]["user_confirmed"] = True
        first_draft.status = AutoApplyDraft.Status.FAILED
        first_draft.save()

        drifted_schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Gender",
                    field_type=SINGLE_SELECT,
                    required=False,
                    options=("Man", "Woman", "Decline To Self Identify"),
                    options_complete=True,
                ),
            )
        )
        retry_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=drifted_schema), llm_client=FakeLLMClient(),
        )

        entry = retry_draft.answers["Gender"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])

    def test_unconfirmed_prior_answer_is_not_reused(self):
        # A blank/needs_review entry on the prior draft was never actually
        # confirmed by the user -- must not be treated as ground truth.
        schema = self._gender_schema()
        first_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        first_draft.status = AutoApplyDraft.Status.FAILED
        first_draft.save()

        retry_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )

        entry = retry_draft.answers["Gender"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])

    def test_confirmed_combobox_answer_not_reused_when_complete_options_drop_it(self):
        # Regression test for a CodeRabbit finding on PR #99: the original
        # option-revalidation only covered SINGLE_SELECT/MULTI_SELECT/
        # CHECKBOX_GROUP, silently skipping COMBOBOX_SELECT entirely --
        # a confirmed combobox value that's no longer in a now-COMPLETE
        # option set must not be carried forward and marked reviewed.
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Discipline",
                    field_type=COMBOBOX_SELECT,
                    required=False,
                    options=("Computer Science", "Mathematics"),
                    options_complete=True,
                ),
            )
        )
        first_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        first_draft.answers["Discipline"]["value"] = "Computer Science"
        first_draft.answers["Discipline"]["needs_review"] = False
        first_draft.answers["Discipline"]["user_confirmed"] = True
        first_draft.status = AutoApplyDraft.Status.FAILED
        first_draft.save()

        drifted_schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Discipline",
                    field_type=COMBOBOX_SELECT,
                    required=False,
                    options=("Mathematics", "Physics"),  # complete -- CS dropped
                    options_complete=True,
                ),
            )
        )
        retry_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=drifted_schema), llm_client=FakeLLMClient(),
        )

        entry = retry_draft.answers["Discipline"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])

    def test_confident_llm_answer_without_human_review_is_not_reused(self):
        # Regression test for a CodeRabbit finding on PR #99: a confident
        # LLM answer gets needs_review=False too (see llm/base.py), with no
        # human ever having looked at it. Reusing it on a retry would
        # silently present an unreviewed LLM guess as something the user
        # vouched for. Only edit_auto_apply_draft's save path sets
        # `user_confirmed`, never drafting itself.
        schema = self._gender_schema()
        first_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )
        # Simulate a confident LLM answer landing with needs_review=False,
        # but with no user_confirmed marker (drafting never sets one).
        first_draft.answers["Gender"]["value"] = "Male"
        first_draft.answers["Gender"]["needs_review"] = False
        first_draft.status = AutoApplyDraft.Status.FAILED
        first_draft.save()

        retry_draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient(),
        )

        entry = retry_draft.answers["Gender"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])


class RequiredFileQuestionUnanswerableTests(DraftingServiceTestCase):
    """A required FILE-type custom question (e.g. "upload your portfolio")
    is the one custom-question case that still excludes the draft, unlike
    every other unanswerable-required case above. `edit_auto_apply_draft`
    deliberately never lets a human edit a FILE-type answer (a user-supplied
    string there would flow straight into Playwright's set_input_files()),
    so a blank FILE placeholder would be a permanent, unfillable blocker
    rather than something the review queue can actually resolve."""

    def test_required_unanswerable_file_question_still_excludes_draft(self):
        schema = FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label="Upload your portfolio",
                    field_type=FILE,
                    required=True,
                ),
            )
        )
        form_client = FakeFormClient(schema=schema)
        llm_client = FakeLLMClient()  # never resolves a real answer for this field

        draft = draft_for(self.user, self.job, form_client=form_client, llm_client=llm_client)

        self.assertEqual(draft.status, AutoApplyDraft.Status.EXCLUDED)
        self.assertIn("Upload your portfolio", draft.exclusion_reason)
        self.assertNotIn("Upload your portfolio", draft.answers)

    def test_optional_unanswerable_file_question_remains_sendable(self):
        schema = FormSchema(fields=STANDARD_ONLY_SCHEMA.fields + (
            FormField("Upload your portfolio", FILE, False),
        ))
        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema), llm_client=FakeLLMClient(),
        )
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["Upload your portfolio"]
        self.assertEqual(entry["value"], "")
        self.assertFalse(entry["required"])


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
        self.assertTrue(entry["required"])
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
        self.assertTrue(entry["required"])
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

        # Must not crash, and it must not be silently dropped either (R1) --
        # an optional, unanswerable question still gets a visible,
        # needs_review placeholder entry so a human reviewing the draft can
        # see the question existed, even though it doesn't block sending.
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["What is your favorite part of being an engineer?"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertFalse(entry["required"])
        self.assertEqual(entry["reason"], "insufficient_evidence")

    def test_required_llm_eligible_question_with_no_resume_text_drafts_for_manual_review(self):
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

        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        entry = draft.answers["What is your favorite part of being an engineer?"]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertTrue(entry["required"])
        self.assertEqual(entry["reason"], "insufficient_evidence")


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
                if required:
                    # Excludes the draft -- a blank, permanently-unfillable
                    # FILE placeholder would block every future send (see
                    # draft_for's docstring on this exception).
                    self.assertNotIn("Cover letter", draft.answers)
                else:
                    # Floats to the review queue as a blank placeholder,
                    # same as any other optional unanswerable field (R5) --
                    # never silently omitted.
                    self.assertEqual(draft.answers["Cover letter"]["value"], "")
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
                AnswerBank.objects.all().delete()  # the previous subtest's backfilled row
                seed_explicit_answer(user=self.user, category=saved, answer_text="Yes")
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


class AnswerBankDraftingTests(DraftingServiceTestCase):
    """A drafted answer that came from the AnswerBank carries its
    provenance; one that did not keeps exactly its pre-AnswerBank shape."""

    QUESTION = "Are you legally authorized to work in the United States?"

    def _schema(self):
        return FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (
                FormField(
                    label=self.QUESTION,
                    field_type=SINGLE_SELECT,
                    required=True,
                    options=("Yes", "No"),
                ),
            )
        )

    def test_unconfirmed_bank_answer_is_flagged_with_provenance_and_skips_the_llm(self):
        from apps.accounts.services.answer_resolver import write_answer

        write_answer(self.profile, self.QUESTION, "Yes", "learned", options=("Yes", "No"))
        llm_client = FakeLLMClient()

        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=self._schema()), llm_client=llm_client,
        )

        entry = draft.answers[self.QUESTION]
        self.assertEqual(entry["value"], "Yes")
        self.assertTrue(entry["needs_review"])
        self.assertTrue(entry["needs_confirmation"])
        self.assertEqual(entry["tier"], "t0_legal")
        self.assertEqual(entry["provenance"]["source"], "learned")
        self.assertEqual(llm_client.calls, [])

    def test_user_bank_answer_needs_no_confirmation(self):
        from apps.accounts.services.answer_resolver import write_answer

        write_answer(self.profile, self.QUESTION, "Yes", "user", options=("Yes", "No"))

        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=self._schema()), llm_client=FakeLLMClient(),
        )

        entry = draft.answers[self.QUESTION]
        self.assertFalse(entry["needs_review"])
        self.assertFalse(entry["needs_confirmation"])

    def test_answers_without_a_bank_row_keep_the_old_entry_shape(self):
        seed_explicit_answer(
            user=self.user,
            category=ExplicitAnswer.Category.WORK_AUTHORIZATION,
            answer_text="Yes",
        )
        draft = draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=self._schema()), llm_client=FakeLLMClient(),
        )
        entry = draft.answers[self.QUESTION]
        for key in ("needs_confirmation", "provenance", "tier"):
            self.assertNotIn(key, entry)


class RegionalSalaryDraftTests(DraftingServiceTestCase):
    """The profile's salary band answers a salary question straight from the
    resolver, using the job's own country (`Job.location_country`)."""

    QUESTION = "What is your desired salary?"

    def _schema(self):
        return FormSchema(
            fields=STANDARD_ONLY_SCHEMA.fields
            + (FormField(label=self.QUESTION, field_type=TEXT, required=True),)
        )

    def _draft(self, country="US", band_region="US", learned=True):
        from apps.accounts.services.answer_resolver import write_answer

        self.job.location_country = country
        self.job.save()
        self.profile.salary_by_region = {band_region: "75-100k"} if band_region else {}
        self.profile.save()
        if learned:
            write_answer(self.profile, self.QUESTION, "learned value", "learned")
        return draft_for(
            self.user, self.job,
            form_client=FakeFormClient(schema=self._schema()), llm_client=FakeLLMClient(),
        )

    def test_profile_band_beats_a_learned_answer_and_records_its_provenance(self):
        entry = self._draft().answers[self.QUESTION]

        self.assertEqual(entry["value"], "$75,000 – $100,000")
        self.assertEqual(entry["reason"], "profile_fact")
        self.assertFalse(entry["needs_review"])
        self.assertFalse(entry["needs_confirmation"])
        self.assertEqual(entry["provenance"]["origin"], "profile.salary_by_region.US")
        self.assertEqual(entry["provenance"]["source"], "user")
        self.assertEqual(entry["provenance"]["detail"], {"region": "US", "band": "75-100k"})

    def test_a_job_with_no_country_does_not_crash_and_leaves_the_field_for_review(self):
        entry = self._draft(country="", learned=False).answers[self.QUESTION]

        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])

    def test_a_job_in_a_region_without_a_band_is_not_given_another_regions_band(self):
        entry = self._draft(country="India", learned=False).answers[self.QUESTION]

        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])

    def test_a_draft_with_no_band_and_no_bank_row_keeps_its_old_shape(self):
        entry = self._draft(band_region=None, learned=False).answers[self.QUESTION]

        for key in ("needs_confirmation", "provenance", "tier"):
            self.assertNotIn(key, entry)
