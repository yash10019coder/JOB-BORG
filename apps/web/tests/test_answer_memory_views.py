"""Remember-on-review and observation capture, end to end through the real
drafting service and the review/send views."""
from unittest import mock

from django.urls import reverse

from apps.accounts.models import AnswerBank, AnswerObservation
from apps.accounts.services.answer_resolver import write_answer
from apps.auto_apply.greenhouse_form.field_mapping import (
    FILE, SINGLE_SELECT, TEXT, TEXTAREA, FormField, FormSchema,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services.drafting import draft_for
from apps.auto_apply.tests.test_drafting_service import (
    STANDARD_ONLY_SCHEMA, FakeFormClient, FakeLLMClient,
)
from apps.employers.models import Employer
from apps.jobs.models import Job
from apps.web.tests.test_auto_apply_views import AutoApplyViewsTestCase

HEARD = "How did you hear about us?"
AUTH = "Are you legally authorized to work in the United States?"
RELOCATE = "Are you willing to relocate?"
ESSAY = "Why do you want to work at Acme?"


class _MemoryBase(AutoApplyViewsTestCase):
    def setUp(self):
        super().setUp()
        self.alice.email = "alice@example.com"
        self.alice.save()
        self.profile = self.alice.profile
        self.profile.full_name = "Alice Smith"
        self.profile.phone = "555-1234"
        self.profile.linkedin_url = "https://linkedin.com/in/alice"
        self.profile.resume_text = "Alice Smith. Engineer."
        self.profile.save()
        self.client = self._client_for(self.alice)

    def job_in(self, country="US", employer=None):
        job = self._job()
        job.location_country = country
        if employer is not None:
            job.employer = employer
        job.save()
        return job

    def draft(self, job, *questions):
        fields = [
            FormField(label, field_type, True, options)
            for label, field_type, options in questions
        ]
        schema = FormSchema(fields=STANDARD_ONLY_SCHEMA.fields + tuple(fields))
        return draft_for(
            self.alice, job, form_client=FakeFormClient(schema=schema), llm_client=FakeLLMClient()
        )

    def review(self, draft, values=None, remember=(), everywhere=(), confirm=()):
        """Post the whole review form the way the template does."""
        values = values or {}
        posted = {}
        for i, (label, entry) in enumerate(draft.answers.items()):
            posted[f"label__{i}"] = label
            if entry.get("field_type") == FILE:
                continue
            posted[f"value__{i}"] = values.get(label, entry["value"])
            if label in remember:
                posted[f"remember__{i}"] = "on"
            if label in everywhere:
                posted[f"everywhere__{i}"] = "on"
            if label in confirm:
                posted[f"confirm__{i}"] = "on"
        return self.client.post(reverse("edit_auto_apply_draft", args=[draft.id]), posted)

    def row(self, key_text, scope=""):
        from apps.accounts.services.answer_resolver import normalize_question_key

        return AnswerBank.objects.filter(
            profile=self.profile, question_key=normalize_question_key(key_text), scope_region=scope
        ).first()


class RememberOnReviewTests(_MemoryBase):
    def test_a_ticked_t2_answer_is_saved_as_the_users_and_fills_the_next_application(self):
        first = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(first, {HEARD: "LinkedIn"}, remember={HEARD})

        row = self.row(HEARD)
        self.assertEqual((row.value, row.source, row.is_locked), ("LinkedIn", "user", False))
        self.assertEqual(row.source_detail["origin"], "review")
        self.assertEqual(row.source_detail["draft_id"], first.pk)

        beta = Employer.objects.create(name="Beta", slug="beta")
        second = self.draft(self.job_in(employer=beta), (HEARD, TEXT, ()))
        entry = second.answers[HEARD]
        self.assertEqual(entry["value"], "LinkedIn")
        self.assertFalse(entry["needs_review"])
        self.assertEqual(entry["provenance"]["source"], "user")

    def test_unticked_writes_nothing(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"})
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_resaving_an_unreviewed_guess_does_not_remember_it(self):
        """A plain Save re-posts every value; an LLM guess must not become 'yours'."""
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        # Pretend the LLM guessed it.
        draft.answers[HEARD].update({"value": "A job board", "needs_review": True, "reason": "ok"})
        draft.save()
        self.review(draft, remember={HEARD})  # value unchanged
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_guess_saved_once_and_saved_again_is_still_not_remembered(self):
        """Any save marks a value confirmed, so a second click must not
        promote an LLM guess to the user's own answer."""
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        draft.answers[HEARD].update({"value": "A job board", "needs_review": True, "reason": "ok"})
        draft.save()
        for _ in range(2):
            draft.refresh_from_db()
            self.review(draft, remember={HEARD})
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_value_the_user_typed_earlier_is_remembered_when_they_tick_later(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"})  # typed and saved, not remembered
        self.assertEqual(AnswerBank.objects.count(), 0)
        draft.refresh_from_db()
        self.review(draft, remember={HEARD})  # later: tick remember, no change
        self.assertEqual(self.row(HEARD).value, "LinkedIn")

    def test_saving_twice_with_remember_does_not_duplicate_history(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"}, remember={HEARD})
        draft.refresh_from_db()
        self.review(draft, remember={HEARD})
        self.assertEqual(AnswerBank.objects.count(), 1)
        from apps.accounts.models import AnswerBankHistory

        self.assertEqual(AnswerBankHistory.objects.count(), 0)

    def test_a_blank_value_is_never_remembered(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: ""}, remember={HEARD})
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_legal_answer_is_remembered_only_when_ticked_and_is_then_locked(self):
        draft = self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        self.review(draft, {AUTH: "Yes"})
        self.assertEqual(AnswerBank.objects.count(), 0)

        self.review(draft, {AUTH: "Yes"}, remember={AUTH})
        row = self.row(AUTH)
        self.assertEqual((row.value, row.source, row.is_locked, row.risk_tier),
                         ("Yes", "user", True, "t0_legal"))

        second = self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        # Draft is a duplicate-active for the same user/job? different job, fine.
        entry = second.answers[AUTH]
        self.assertEqual(entry["value"], "Yes")
        self.assertFalse(entry["needs_confirmation"])

    def test_confirming_a_learned_answer_with_always_use_replaces_it_with_a_locked_user_row(self):
        write_answer(self.profile, AUTH, "Yes", "learned", options=("Yes", "No"))
        draft = self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        self.assertTrue(draft.answers[AUTH]["needs_confirmation"])

        self.review(draft, remember={AUTH}, confirm={AUTH})
        row = self.row(AUTH)
        self.assertEqual((row.source, row.is_locked), ("user", True))

    def test_a_held_answer_that_is_not_confirmed_is_never_remembered(self):
        write_answer(self.profile, AUTH, "Yes", "learned", options=("Yes", "No"))
        draft = self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        self.review(draft, remember={AUTH})  # ticked remember but not confirm
        row = self.row(AUTH)
        self.assertEqual(row.source, "learned")

    def test_a_failed_remember_never_loses_the_edit(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        with mock.patch(
            "apps.auto_apply.services.answer_memory.write_answer", side_effect=RuntimeError("boom")
        ):
            self.review(draft, {HEARD: "LinkedIn"}, remember={HEARD})
        draft.refresh_from_db()
        self.assertEqual(draft.answers[HEARD]["value"], "LinkedIn")

    def test_a_message_says_what_was_remembered(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        response = self.client.post(
            reverse("edit_auto_apply_draft", args=[draft.id]),
            {"label__0": "First Name", "label__5": HEARD,
             **{f"label__{i}": label for i, label in enumerate(draft.answers)},
             **{f"value__{list(draft.answers).index(HEARD)}": "LinkedIn",
                f"remember__{list(draft.answers).index(HEARD)}": "on"}},
            follow=True,
        )
        self.assertContains(response, "Remembered 1 answer")

    def test_file_standard_and_textarea_answers_are_never_remembered(self):
        draft = self.draft(self.job_in(), (ESSAY, TEXTAREA, ()))
        everything = set(draft.answers)
        self.review(draft, {ESSAY: "Because I love it"}, remember=everything)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_the_remembered_row_belongs_to_the_reviewer_only(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"}, remember={HEARD})
        self.assertEqual(AnswerBank.objects.filter(profile=self.bob.profile).count(), 0)

    def test_the_employer_name_is_stripped_from_the_remembered_key(self):
        question = "What excites you about working at Acme?"
        draft = self.draft(self.job_in(), (question, TEXT, ()))
        self.review(draft, {question: "The mission"}, remember={question})
        beta = Employer.objects.create(name="Beta", slug="beta")
        second = self.draft(
            self.job_in(employer=beta), ("What excites you about working at Beta?", TEXT, ())
        )
        self.assertEqual(second.answers["What excites you about working at Beta?"]["value"], "The mission")


class LocationSensitiveMemoryTests(_MemoryBase):
    def test_a_relocation_answer_is_saved_for_the_jobs_region_only(self):
        draft = self.draft(self.job_in("US"), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        self.review(draft, {RELOCATE: "Yes"}, remember={RELOCATE})
        self.assertIsNotNone(self.row(RELOCATE, "US"))
        self.assertIsNone(self.row(RELOCATE, ""))

        india = self.draft(self.job_in("India"), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        self.assertEqual(india.answers[RELOCATE]["value"], "")
        self.assertTrue(india.answers[RELOCATE]["needs_review"])
        us_again = self.draft(self.job_in("US"), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        self.assertEqual(us_again.answers[RELOCATE]["value"], "Yes")

    def test_marking_it_any_region_fills_every_job(self):
        draft = self.draft(self.job_in("US"), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        self.review(draft, {RELOCATE: "Yes"}, remember={RELOCATE}, everywhere={RELOCATE})
        row = self.row(RELOCATE, "")
        self.assertTrue(row.source_detail["applies_everywhere"])
        for country in ("India", "US", "Germany"):
            filled = self.draft(self.job_in(country), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
            self.assertEqual(filled.answers[RELOCATE]["value"], "Yes", country)

    def test_an_unknown_job_region_is_not_guessed(self):
        draft = self.draft(self.job_in(""), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        self.review(draft, {RELOCATE: "Yes"}, remember={RELOCATE})
        self.assertEqual(AnswerBank.objects.count(), 0)


class RememberUiTests(_MemoryBase):
    def queue(self):
        return self.client.get(reverse("auto_apply_queue")).content.decode()

    def test_a_t2_question_offers_a_pre_ticked_remember_box(self):
        self.draft(self.job_in(), (HEARD, TEXT, ()))
        html = self.queue()
        self.assertIn("Remember this answer for similar questions", html)
        self.assertRegex(html, r'name="remember__\d+" checked')

    def test_a_legal_question_offers_an_unticked_always_use_box(self):
        self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        html = self.queue()
        self.assertIn("Always use this answer for this question", html)
        self.assertNotRegex(html, r'name="remember__\d+" checked')

    def test_a_location_sensitive_question_offers_the_any_region_box(self):
        self.draft(self.job_in("US"), (RELOCATE, SINGLE_SELECT, ("Yes", "No")))
        html = self.queue()
        self.assertIn("Use it for jobs in any region", html)
        self.assertIn("otherwise only", html)

    def test_long_text_files_and_standard_fields_offer_nothing(self):
        self.draft(self.job_in(), (ESSAY, TEXTAREA, ()))
        self.assertNotIn("Remember this answer", self.queue())


class ObservationTests(_MemoryBase):
    def send(self, draft):
        with mock.patch("apps.web.views.submit_auto_apply_draft"):
            return self.client.post(reverse("send_auto_apply_draft", args=[draft.id]))

    def test_sending_records_one_observation_per_answered_non_standard_question(self):
        job = self.job_in("US")
        draft = self.draft(job, (HEARD, TEXT, ()), (ESSAY, TEXTAREA, ()))
        self.review(draft, {HEARD: "LinkedIn", ESSAY: "Because"})
        self.send(draft)

        draft.refresh_from_db()
        self.assertEqual(draft.status, AutoApplyDraft.Status.SENDING)
        rows = {o.question_text: o for o in AnswerObservation.objects.all()}
        self.assertEqual(set(rows), {HEARD, ESSAY})
        heard = rows[HEARD]
        self.assertEqual(
            (heard.value, heard.tier, heard.field_type, heard.user_confirmed, heard.job_id,
             heard.draft_id, heard.employer_name, heard.job_region),
            ("LinkedIn", "t2_factual", "text", True, job.pk, draft.pk, "Acme", "US"),
        )
        self.assertEqual(heard.profile, self.profile)

    def test_observations_carry_the_provenance_of_what_was_submitted(self):
        write_answer(self.profile, HEARD, "Referral", "learned")
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.send(draft)
        observation = AnswerObservation.objects.get()
        self.assertEqual(observation.provenance_source, "learned")
        self.assertEqual(observation.provenance_origin, "answer_bank")
        self.assertFalse(observation.user_confirmed)

    def test_blank_standard_and_file_fields_are_not_observed(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: ""})
        # The blank required field blocks sending, so fill it with a space-only
        # optional placeholder instead: nothing answered -> nothing observed.
        draft.answers[HEARD]["required"] = False
        draft.save()
        self.send(draft)
        self.assertEqual(AnswerObservation.objects.count(), 0)

    def test_a_refused_send_records_nothing(self):
        write_answer(self.profile, AUTH, "Yes", "learned", options=("Yes", "No"))
        draft = self.draft(self.job_in(), (AUTH, SINGLE_SELECT, ("Yes", "No")))
        self.send(draft)  # held: unconfirmed
        self.assertEqual(AnswerObservation.objects.count(), 0)

    def test_a_lost_race_records_nothing(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"})

        def concurrent_edit(answers):
            AutoApplyDraft.objects.filter(pk=draft.pk).update(answers={})
            return {"answers": {}}

        with mock.patch("apps.web.views.build_submit_snapshot", side_effect=concurrent_edit), \
                mock.patch("apps.web.views.submit_auto_apply_draft") as task:
            response = self.client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(AnswerObservation.objects.count(), 0)
        task.delay.assert_not_called()

    def test_observations_belong_to_the_sender_only(self):
        draft = self.draft(self.job_in(), (HEARD, TEXT, ()))
        self.review(draft, {HEARD: "LinkedIn"})
        self.send(draft)
        self.assertEqual(AnswerObservation.objects.filter(profile=self.bob.profile).count(), 0)
        self.assertEqual(AnswerObservation.objects.filter(profile=self.profile).count(), 1)

    def test_narrative_observations_keep_the_employer_in_their_key(self):
        draft = self.draft(self.job_in(), (ESSAY, TEXTAREA, ()))
        self.review(draft, {ESSAY: "Because"})
        self.send(draft)
        self.assertIn("acme", AnswerObservation.objects.get().question_key)
