"""Candidates for reworded questions in the draft review (Phase 4b, #130), end to
end through the real drafting service and the review/send views.

The central invariant (plan R2): a candidate is only ever shown. It never
changes a field's value, its review flag, its confirmation flag or the draft's
required-field outcome.
"""
import json
from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import AnswerBank
from apps.accounts.services.answer_resolver import write_answer
from apps.accounts.tiering import Tier, classify_tier
from apps.auto_apply.greenhouse_form.field_mapping import (
    FILE, SINGLE_SELECT, TEXT, TEXTAREA, FormField, FormSchema,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services.confirmation import unconfirmed_fields
from apps.auto_apply.services.drafting import _attach_candidates, draft_for
from apps.auto_apply.tests.test_drafting_service import STANDARD_ONLY_SCHEMA, FakeFormClient, FakeLLMClient
from apps.web.tests.test_answer_memory_views import _MemoryBase

HEARD_OLD = "How did you hear about us?"
HEARD_NEW = "Where did you hear about this job?"
NOTICE_OLD = "What is your notice period?"
NOTICE_NEW = "Notice period?"
PAIRS = Path(__file__).resolve().parents[2] / "accounts" / "tests" / "fixtures" / "question_pairs.json"


@override_settings(QUESTION_CANDIDATES_ENABLED=True)
class _Base(_MemoryBase):
    def accept(self, draft, label, key, **extra):
        posted = {}
        for i, (lab, entry) in enumerate(draft.answers.items()):
            posted[f"label__{i}"] = lab
            if entry.get("field_type") == FILE:
                continue
            posted[f"value__{i}"] = entry["value"] if isinstance(entry["value"], str) else ""
            if lab == label:
                posted[f"accept__{i}"] = key
        posted.update(extra)
        return self.client.post(reverse("edit_auto_apply_draft", args=[draft.id]), posted)

    def fresh(self, draft):
        return AutoApplyDraft.objects.get(pk=draft.pk)


class AttachTests(_Base):
    def test_a_reworded_question_gets_the_earlier_answer_beside_a_blank_field(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        entry = draft.answers[HEARD_NEW]
        self.assertEqual(entry["value"], "")
        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["candidates"][0]["value"], "LinkedIn")
        self.assertEqual(entry["candidates"][0]["question_text"], HEARD_OLD)

    def test_off_setting_leaves_the_entry_byte_identical(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        with override_settings(QUESTION_CANDIDATES_ENABLED=False):
            draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.assertNotIn("candidates", draft.answers[HEARD_NEW])

    def test_no_candidates_for_filled_long_text_or_file_fields(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        payload = {
            "filled": {"value": "x", "field_type": "text"},
            "essay": {"value": "", "field_type": TEXTAREA},
            "upload": {"value": "", "field_type": FILE},
        }
        _attach_candidates(self.profile, self.job_in(), payload)
        self.assertTrue(all("candidates" not in entry for entry in payload.values()))

    def test_a_field_with_an_answer_provenance_gets_no_candidates(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        entry = {"value": "", "field_type": "text", "provenance": {"origin": "answer_bank"}}
        _attach_candidates(self.profile, self.job_in(), {HEARD_NEW: entry})
        self.assertNotIn("candidates", entry)

    def test_a_matcher_that_raises_leaves_the_draft_as_without_candidates(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        with mock.patch("apps.auto_apply.services.drafting.find_candidates", side_effect=RuntimeError("boom")):
            draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertNotIn("candidates", draft.answers[HEARD_NEW])

    def test_the_failure_log_carries_no_question_text_or_values(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        with mock.patch("apps.auto_apply.services.drafting.find_candidates", side_effect=RuntimeError("LinkedIn")):
            with self.assertLogs("apps.auto_apply.services.drafting", level="WARNING") as logs:
                self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        joined = " ".join(logs.output)
        self.assertNotIn("LinkedIn", joined)
        self.assertNotIn(HEARD_NEW, joined)


class InvariantTests(_Base):
    """R2 over every reworded pair in the labelled set: candidates on vs off."""

    def pairs(self):
        data = json.loads(PAIRS.read_text())
        out = []
        for half in ("tuning", "held_out"):
            out.extend(data[half]["reworded"])
        return out

    def test_a_candidate_never_changes_value_review_confirmation_or_outcome(self):
        pairs = self.pairs()
        questions = [(p["b"], TEXT, ()) for p in pairs]
        for p in pairs:
            write_answer(self.profile, p["a"], "SAVED", "user")

        with override_settings(QUESTION_CANDIDATES_ENABLED=False):
            off = self.draft(self.job_in(), *questions)
        off_answers, off_status = json.loads(json.dumps(off.answers)), off.status
        off.delete()
        on = self.draft(self.job_in(), *questions)

        self.assertEqual(on.status, off_status)
        with_candidates = 0
        for label, entry in json.loads(json.dumps(on.answers)).items():
            stripped = {k: v for k, v in entry.items() if k != "candidates"}
            self.assertEqual(stripped, off_answers[label], label)
            with_candidates += "candidates" in entry
        # the property above is only meaningful if candidates were really attached
        self.assertGreaterEqual(with_candidates, len(pairs) // 2)

    def test_legal_and_commercial_questions_are_covered_by_the_corpus(self):
        shapes = json.loads(PAIRS.read_text())["tier_shapes"]
        tiers = {classify_tier(q) for q in shapes}
        self.assertTrue({Tier.T0_LEGAL, Tier.T1_COMMERCIAL} <= tiers)


class ReviewPageTests(_Base):
    def test_the_review_page_shows_the_earlier_question_and_an_empty_input(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        response = self.client.get(reverse("auto_apply_queue"))
        self.assertContains(response, "You answered similar questions before")
        self.assertContains(response, HEARD_OLD)
        self.assertContains(response, "Use this answer")
        # nothing is prefilled: the control for the field renders no value
        self.assertNotContains(response, 'value="LinkedIn"')
        self.assertEqual(self.fresh(draft).answers[HEARD_NEW]["value"], "")

    def test_the_first_submit_button_is_save_so_enter_cannot_accept(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        html = self.client.get(reverse("auto_apply_queue")).content.decode()
        self.assertLess(html.index("Save answers"), html.index("Use this answer"))

    def test_a_value_that_does_not_fit_the_choices_is_text_only(self):
        write_answer(self.profile, NOTICE_OLD, "4 weeks", "user")
        self.draft(self.job_in(), (NOTICE_NEW, SINGLE_SELECT, ("Immediately", "1 month")))
        html = self.client.get(reverse("auto_apply_queue")).content.decode()
        self.assertIn("does not match this form", html)
        self.assertNotIn("Use this answer", html)

    def test_a_legal_or_commercial_candidate_says_it_still_needs_confirmation(self):
        write_answer(self.profile, NOTICE_OLD, "4 weeks", "user")
        self.draft(self.job_in(), (NOTICE_NEW, TEXT, ()))
        self.assertContains(self.client.get(reverse("auto_apply_queue")), "still need to confirm it before sending")


class AcceptTests(_Base):
    def test_accepting_a_t2_candidate_fills_the_field_without_marking_it_typed(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key)
        entry = self.fresh(draft).answers[HEARD_NEW]
        self.assertEqual(entry["value"], "LinkedIn")
        self.assertTrue(entry["candidate_accepted"])
        self.assertNotIn("user_edited", entry)
        self.assertFalse(entry["needs_review"])
        self.assertFalse(entry.get("needs_confirmation"))

    def test_accepting_with_always_use_saves_the_new_wording_as_the_users_answer(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        i = list(draft.answers).index(HEARD_NEW)
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key, **{f"remember__{i}": "on"})
        row = self.row(HEARD_NEW)
        self.assertEqual((row.value, row.source), ("LinkedIn", "user"))
        self.assertTrue(row.source_detail["via_candidate"])
        second = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.assertEqual(second.answers[HEARD_NEW]["value"], "LinkedIn")

    def test_accepting_a_legal_or_commercial_candidate_leaves_it_held_for_confirmation(self):
        write_answer(self.profile, NOTICE_OLD, "4 weeks", "user")
        draft = self.draft(self.job_in(), (NOTICE_NEW, TEXT, ()))
        key = draft.answers[NOTICE_NEW]["candidates"][0]["question_key"]
        self.accept(draft, NOTICE_NEW, key)
        entry = self.fresh(draft).answers[NOTICE_NEW]
        self.assertEqual(entry["value"], "4 weeks")
        self.assertTrue(entry["needs_confirmation"])
        self.assertTrue(entry["needs_review"])
        self.assertFalse(entry["user_confirmed"])
        self.assertEqual(entry["provenance"]["origin"], "answer_candidate")
        self.assertIn(NOTICE_NEW, unconfirmed_fields(self.fresh(draft).answers))

    def test_a_held_candidate_cannot_be_sent_until_confirmed(self):
        write_answer(self.profile, NOTICE_OLD, "4 weeks", "user")
        draft = self.draft(self.job_in(), (NOTICE_NEW, TEXT, ()))
        key = draft.answers[NOTICE_NEW]["candidates"][0]["question_key"]
        self.accept(draft, NOTICE_NEW, key)
        with mock.patch("apps.web.views.submit_auto_apply_draft") as submit:
            self.client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        submit.delay.assert_not_called()
        self.assertEqual(self.fresh(draft).status, AutoApplyDraft.Status.DRAFTED)

    def test_the_bulk_confirm_does_not_confirm_an_accepted_candidate(self):
        write_answer(self.profile, NOTICE_OLD, "4 weeks", "user")
        draft = self.draft(self.job_in(), (NOTICE_NEW, TEXT, ()))
        key = draft.answers[NOTICE_NEW]["candidates"][0]["question_key"]
        self.accept(draft, NOTICE_NEW, key)
        self.client.post(reverse("confirm_learned_answers", args=[draft.id]))
        self.assertTrue(self.fresh(draft).answers[NOTICE_NEW]["needs_confirmation"])

    def test_editing_an_accepted_value_makes_it_the_users_own(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key)
        accepted = self.fresh(draft)
        self.review(accepted, {HEARD_NEW: "A friend"})
        entry = self.fresh(draft).answers[HEARD_NEW]
        self.assertEqual(entry["value"], "A friend")
        self.assertTrue(entry["user_edited"])
        self.assertNotIn("candidate_accepted", entry)

    def test_resaving_an_accepted_value_unchanged_keeps_it_not_typed(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key)
        self.review(self.fresh(draft))
        entry = self.fresh(draft).answers[HEARD_NEW]
        self.assertTrue(entry["candidate_accepted"])
        self.assertNotIn("user_edited", entry)

    def test_a_made_up_or_other_profiles_key_selects_nothing(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        write_answer(self.bob.profile, "How did you find out about this role?", "Twitter", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        bobs_key = self.bob.profile.answer_bank.get().question_key
        for key in ("nonsense", bobs_key, ""):
            self.accept(draft, HEARD_NEW, key)
            entry = self.fresh(draft).answers[HEARD_NEW]
            self.assertEqual(entry["value"], "", key)
            self.assertNotIn("candidate_accepted", entry)

    def test_a_posted_value_cannot_smuggle_a_candidate_in(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        # a tampered form that names a key AND posts a different value: the
        # server uses the re-derived stored answer, never the posted one, for an accept
        i = list(draft.answers).index(HEARD_NEW)
        self.accept(draft, HEARD_NEW, key, **{f"value__{i}": "Evil"})
        self.assertEqual(self.fresh(draft).answers[HEARD_NEW]["value"], "LinkedIn")

    def test_another_users_draft_is_a_404(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        bob = self._client_for(self.bob)
        response = bob.post(
            reverse("edit_auto_apply_draft", args=[draft.id]),
            {"label__0": HEARD_NEW, "value__0": "", "accept__0": key},
        )
        self.assertEqual(response.status_code, 404)

    def test_accepting_into_a_field_that_already_has_an_answer_does_nothing(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        draft.answers[HEARD_NEW]["value"] = "Mine"
        draft.save()
        self.accept(self.fresh(draft), HEARD_NEW, key)
        self.assertEqual(self.fresh(draft).answers[HEARD_NEW]["value"], "Mine")

    def test_accepting_after_the_feature_is_switched_off_does_nothing(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        with override_settings(QUESTION_CANDIDATES_ENABLED=False):
            self.accept(draft, HEARD_NEW, key)
        self.assertEqual(self.fresh(draft).answers[HEARD_NEW]["value"], "")


class SendTests(_Base):
    def test_candidates_are_not_kept_on_a_sent_draft_or_in_the_snapshot(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()), ("Essay", TEXTAREA, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key)
        # fill the other required field so the send gate opens
        accepted = self.fresh(draft)
        self.review(accepted, {"Essay": "text"})
        with mock.patch("apps.web.views.submit_auto_apply_draft"):
            self.client.post(reverse("send_auto_apply_draft", args=[draft.id]))
        sent = self.fresh(draft)
        self.assertEqual(sent.status, AutoApplyDraft.Status.SENDING)
        self.assertTrue(all("candidates" not in e for e in sent.answers.values() if isinstance(e, dict)))
        self.assertNotIn("candidates", json.dumps(sent.submitted_answers_snapshot))


class ObservationTests(_Base):
    """What was shown and how the value arrived is recorded when a draft is sent (plan U4)."""

    def send(self, draft):
        with mock.patch("apps.web.views.submit_auto_apply_draft"):
            self.client.post(reverse("send_auto_apply_draft", args=[draft.id]))

    def observed(self, label):
        from apps.accounts.models import AnswerObservation

        return AnswerObservation.objects.get(question_text=label)

    def test_an_accepted_candidate_is_recorded_as_accepted_and_equal(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        key = draft.answers[HEARD_NEW]["candidates"][0]["question_key"]
        self.accept(draft, HEARD_NEW, key)
        self.send(draft)
        obs = self.observed(HEARD_NEW)
        self.assertEqual(obs.candidate_question_key, key)
        self.assertTrue(obs.candidate_accepted)
        self.assertTrue(obs.candidate_equal)
        self.assertFalse(obs.user_edited)

    def test_a_typed_answer_that_matches_the_shown_candidate_is_equal_but_not_accepted(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.review(draft, {HEARD_NEW: "  linkedin "})
        self.send(draft)
        obs = self.observed(HEARD_NEW)
        self.assertFalse(obs.candidate_accepted)
        self.assertTrue(obs.candidate_equal)
        self.assertTrue(obs.user_edited)

    def test_a_typed_answer_that_differs_is_recorded_as_not_equal(self):
        write_answer(self.profile, HEARD_OLD, "LinkedIn", "user")
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.review(draft, {HEARD_NEW: "A friend"})
        self.send(draft)
        obs = self.observed(HEARD_NEW)
        self.assertEqual((obs.candidate_equal, obs.candidate_accepted), (False, False))
        self.assertNotEqual(obs.candidate_question_key, "")

    def test_a_question_with_no_candidate_stores_nothing_extra(self):
        draft = self.draft(self.job_in(), (HEARD_NEW, TEXT, ()))
        self.review(draft, {HEARD_NEW: "A friend"})
        self.send(draft)
        obs = self.observed(HEARD_NEW)
        self.assertEqual((obs.candidate_question_key, obs.candidate_equal, obs.candidate_accepted), ("", None, False))
