from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import AnswerBank
from apps.accounts.services import answer_resolver
from apps.accounts.services.answer_resolver import write_answer
from apps.accounts.services.panel_cache import panel_cache_key
from apps.auto_apply.greenhouse_form.field_mapping import (
    CHECKBOX_GROUP, FILE, MULTI_SELECT, SINGLE_SELECT, TEXT, TEXTAREA, FormField, FormSchema,
    schema_to_dict,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services import questions_panel
from apps.auto_apply.tests.test_questions_panel import PanelTestCase
from django.contrib.auth import get_user_model

User = get_user_model()

HEARD = "How did you hear about us?"
EEO = "Are you a veteran?"
RELOCATE = "Are you willing to relocate?"
AUTH = "Are you legally authorized to work in the United States?"


class QuickFillTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def make(self, field, entry=None, **kwargs):
        draft = self.draft(field, **kwargs)
        if entry is not None:
            AutoApplyDraft.objects.filter(pk=draft.pk).update(answers={field.label: entry})
        return draft

    def url(self, draft, index=0):
        return f"{reverse('quick_fill')}?draft={draft.pk}&field={index}"

    def post(self, draft, index=0, **data):
        return self.client.post(
            reverse("quick_fill"), {"draft": draft.pk, "field": index, **data}
        )

    def row(self, key_text, scope=""):
        return AnswerBank.objects.filter(
            profile=self.profile, question_key=answer_resolver.normalize_question_key(key_text),
            scope_region=scope,
        ).first()


class FlowTests(QuickFillTests):
    def test_get_shows_the_question_the_proposed_answer_and_where_it_was_asked(self):
        draft = self.make(
            FormField(HEARD, TEXT, False, ()),
            {"value": "A friend", "provenance": {"source": "learned"}},
        )
        response = self.client.get(self.url(draft))
        self.assertContains(response, HEARD)
        self.assertContains(response, 'value="A friend"')
        self.assertContains(response, "learned")
        self.assertContains(response, "Engineer at Acme")

    def test_a_user_value_writes_a_locked_user_row_recording_its_draft_and_origin(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        response = self.post(draft, value="LinkedIn")
        self.assertRedirects(
            response, reverse("profile_answers") + "#panel", fetch_redirect_response=False
        )
        row = self.row(HEARD)
        self.assertEqual((row.value, row.source, row.is_locked), ("LinkedIn", "user", True))
        self.assertEqual(row.source_detail["origin"], "quick_fill")
        self.assertEqual(row.source_detail["draft_id"], draft.pk)

    def test_the_panel_then_shows_the_question_answered(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        self.assertFalse(self.rows()[HEARD]["answered"])
        cache.set(panel_cache_key(self.user.pk), questions_panel.build_panel(self.user), 3600)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(draft, value="LinkedIn")
        self.assertTrue(questions_panel.get_panel(self.user)["groups"][-1]["rows"][0]["answered"])

    def test_the_saved_answer_fills_the_same_question_next_time(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        self.post(draft, value="LinkedIn")
        found = answer_resolver.resolve_answer(self.profile, HEARD, self.job)
        self.assertEqual(found.value, "LinkedIn")


class ConfirmationRuleTests(QuickFillTests):
    """FR6.5: a legal/commercial value that did not come from the user cannot
    be saved (and locked) without its own explicit confirmation."""

    def legal(self, source="learned", **entry):
        entry = {"value": "No", "provenance": {"source": source}, **entry}
        return self.make(FormField(EEO, SINGLE_SELECT, False, ("Yes", "No")), entry)

    def test_a_non_user_t0_prefill_without_the_confirm_box_writes_nothing(self):
        draft = self.legal()
        response = self.post(draft, value="No")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tick the box to confirm")
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_the_confirm_box_lets_it_through(self):
        draft = self.legal()
        self.post(draft, value="No", confirm_value="on")
        row = self.row(EEO)
        self.assertEqual((row.value, row.source, row.is_locked, row.risk_tier),
                         ("No", "user", True, "t0_legal"))
        self.assertEqual(row.source_detail["prefill_provenance"]["source"], "learned")

    def test_an_llm_prefill_needs_the_box_too(self):
        draft = self.legal(source="llm")
        self.assertEqual(self.post(draft, value="No").status_code, 200)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_the_page_shows_the_confirm_box_only_when_needed(self):
        needs = self.legal()
        self.assertContains(self.client.get(self.url(needs)), 'name="confirm_value"')
        mine = self.legal(source="user", user_confirmed=True)
        self.assertNotContains(self.client.get(self.url(mine)), 'name="confirm_value"')

    def test_a_value_the_user_already_confirmed_needs_no_box(self):
        draft = self.legal(source="llm", user_confirmed=True)
        self.post(draft, value="No")
        self.assertEqual(self.row(EEO).source, "user")

    def test_a_t2_question_needs_no_confirmation_even_from_an_llm_prefill(self):
        draft = self.make(
            FormField(HEARD, TEXT, False, ()), {"value": "A job board", "provenance": {"source": "llm"}}
        )
        self.post(draft, value="A job board")
        self.assertEqual(self.row(HEARD).value, "A job board")

    def test_typing_a_new_value_into_an_empty_t0_question_still_needs_no_box(self):
        draft = self.make(FormField(EEO, SINGLE_SELECT, False, ("Yes", "No")))
        self.post(draft, value="No")
        self.assertEqual(self.row(EEO).value, "No")


class ValidationTests(QuickFillTests):
    def test_a_blank_answer_is_rejected(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        self.assertEqual(self.post(draft, value="  ").status_code, 200)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_select_answer_must_be_one_of_the_options(self):
        draft = self.make(FormField(HEARD, SINGLE_SELECT, False, ("Friend", "Ad")))
        response = self.post(draft, value="Podcast")
        self.assertContains(response, "Choose one of this form")
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.post(draft, value="Friend")
        self.assertEqual(self.row(HEARD).value, "Friend")

    def test_multi_select_answers_must_all_be_options(self):
        draft = self.make(FormField("Which languages?", MULTI_SELECT, False, ("Go", "Rust")))
        self.assertEqual(self.post(draft, value=["Go", "Perl"]).status_code, 200)
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.post(draft, value=["Go", "Rust"])
        self.assertEqual(AnswerBank.objects.get().value, ["Go", "Rust"])

    def test_checkbox_groups_are_validated_like_multi_selects(self):
        draft = self.make(FormField("Pick any", CHECKBOX_GROUP, False, ("A", "B")))
        self.assertEqual(self.post(draft, value=["C"]).status_code, 200)

    def test_an_invalid_post_keeps_what_the_user_typed(self):
        write_answer(self.profile, HEARD, "Original", "user")
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        response = self.post(draft, value="Something new")  # existing answer, no overwrite
        self.assertContains(response, 'value="Something new"')


class ExistingAnswerTests(QuickFillTests):
    def test_an_existing_locked_answer_is_not_replaced_without_overwrite(self):
        write_answer(self.profile, HEARD, "Original", "user", is_locked=True)
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        response = self.post(draft, value="New")
        self.assertContains(response, "Tick the box to replace it")
        self.assertEqual(self.row(HEARD).value, "Original")

    def test_overwrite_replaces_it_and_keeps_history(self):
        write_answer(self.profile, HEARD, "Original", "user", is_locked=True)
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        self.post(draft, value="New", overwrite="on")
        self.assertEqual(self.row(HEARD).value, "New")
        from apps.accounts.models import AnswerBankHistory

        self.assertEqual(AnswerBankHistory.objects.get().value, "Original")

    def test_the_page_warns_about_an_existing_answer(self):
        write_answer(self.profile, HEARD, "Original", "user")
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        page = self.client.get(self.url(draft))
        self.assertContains(page, "You already have an answer")
        self.assertContains(page, "Original")


class RefusalTests(QuickFillTests):
    def test_settings_backed_questions_are_refused_and_written_nowhere(self):
        draft = self.make(FormField(AUTH, SINGLE_SELECT, True, ("Yes", "No")), {"value": "Yes"})
        response = self.client.post(
            reverse("quick_fill"), {"draft": draft.pk, "field": 0, "value": "Yes", "confirm_value": "on"},
            follow=True,
        )
        self.assertContains(response, "answered from your settings")
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.assertEqual(self.user.profile.visa_status_by_country, {})

    def test_citizenship_and_location_membership_are_refused_too(self):
        for label in ("Are you a US citizen?", "Are you currently located in the US?"):
            draft = self.make(
                FormField(label, SINGLE_SELECT, True, ("Yes", "No")), job=self._job(label)
            )
            self.post(draft, value="Yes")
            self.assertEqual(AnswerBank.objects.count(), 0, label)

    def test_long_text_and_files_are_refused(self):
        for field in (FormField("Why Acme?", TEXTAREA, True, ()), FormField("Resume", FILE, True, ())):
            draft = self.make(field, job=self._job(field.label))
            response = self.client.post(
                reverse("quick_fill"), {"draft": draft.pk, "field": 0, "value": "x"}, follow=True
            )
            self.assertContains(response, "be saved for reuse")
        self.assertEqual(AnswerBank.objects.count(), 0)


class SecurityTests(QuickFillTests):
    def test_another_users_draft_is_404(self):
        bob = User.objects.create_user(username="bob", password="pw")
        draft = self.make(FormField(HEARD, TEXT, False, ()), user=bob, job=self._job("bob"))
        self.assertEqual(self.client.get(self.url(draft)).status_code, 404)
        self.assertEqual(self.post(draft, value="x").status_code, 404)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_bad_or_missing_ids_are_404(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        for query in (
            "", "?draft=abc&field=0", f"?draft={draft.pk}&field=9", f"?draft={draft.pk}&field=-1",
            f"?draft={draft.pk}", "?draft=999999&field=0",
        ):
            self.assertEqual(self.client.get(reverse("quick_fill") + query).status_code, 404, query)

    def test_the_question_and_options_come_from_the_draft_not_the_request(self):
        draft = self.make(FormField(HEARD, SINGLE_SELECT, False, ("Friend", "Ad")))
        self.post(draft, value="Friend", label="Something else", options="x")
        self.assertEqual(self.row(HEARD).value, "Friend")
        self.assertIsNone(self.row("Something else"))

    def test_anonymous_is_sent_to_login_and_csrf_is_enforced(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        self.assertEqual(Client().get(self.url(draft)).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse("quick_fill"), {"draft": draft.pk, "field": 0}).status_code, 403)


class LocationSensitiveTests(QuickFillTests):
    def relocate_draft(self, country="US"):
        return self.make(
            FormField(RELOCATE, SINGLE_SELECT, False, ("Yes", "No")), job=self._job("r" + country, country)
        )

    def test_it_is_saved_for_the_drafts_region_by_default(self):
        self.post(self.relocate_draft("US"), value="Yes")
        self.assertIsNotNone(self.row(RELOCATE, "US"))
        self.assertIsNone(self.row(RELOCATE, ""))

    def test_any_region_stores_a_global_row_marked_everywhere(self):
        self.post(self.relocate_draft("US"), value="Yes", everywhere="on")
        row = self.row(RELOCATE, "")
        self.assertTrue(row.source_detail["applies_everywhere"])

    def test_an_unknown_region_requires_any_region(self):
        draft = self.relocate_draft("")
        self.assertEqual(self.post(draft, value="Yes").status_code, 200)
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.post(draft, value="Yes", everywhere="on")
        self.assertEqual(AnswerBank.objects.count(), 1)

    def test_the_existing_check_is_per_region(self):
        write_answer(self.profile, RELOCATE, "No", "user", scope_region="IN")
        draft = self.relocate_draft("US")
        self.post(draft, value="Yes")
        self.assertEqual(self.row(RELOCATE, "IN").value, "No")
        self.assertEqual(self.row(RELOCATE, "US").value, "Yes")


class PanelPageTests(QuickFillTests):
    def test_the_answers_page_lists_questions_with_quick_fill_links(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, 'id="panel"')
        self.assertContains(page, HEARD)
        self.assertContains(page, f"{reverse('quick_fill')}?draft={draft.pk}&field=0")

    def test_settings_backed_rows_link_to_the_setting_not_to_quick_fill(self):
        self.make(FormField(AUTH, SINGLE_SELECT, True, ("Yes", "No")))
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, 'href="#work-auth"')
        self.assertNotContains(page, reverse("quick_fill") + "?draft=")

    def test_an_empty_panel_says_so(self):
        self.assertContains(self.client.get(reverse("profile_answers")), "Nothing yet")

    def test_discarding_a_draft_refreshes_the_panel(self):
        draft = self.make(FormField(HEARD, TEXT, False, ()))
        AutoApplyDraft.objects.filter(pk=draft.pk).update(status=AutoApplyDraft.Status.DRAFTED)
        key = panel_cache_key(self.user.pk)
        cache.set(key, {"stale": True}, 3600)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("discard_auto_apply_draft", args=[draft.pk]))
        self.assertIsNone(cache.get(key))
