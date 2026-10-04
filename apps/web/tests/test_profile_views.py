from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import AnswerBank, AnswerBankHistory, Profile
from apps.accounts.services.answer_resolver import write_answer
from apps.accounts.services.panel_cache import panel_cache_key

User = get_user_model()

ANSWERS_BASE = {
    "full_name": "", "phone": "", "linkedin_url": "", "github_url": "",
    "portfolio_url": "", "current_employer": "", "location_city": "",
    "location_country": "", "mailing_address": "", "working_timezone": "",
    "visa_rows_present": "1", "citizenship_rows_present": "1",
}
SEARCH_BASE = {
    "headline": "", "target_titles": "", "target_tags": "", "target_locations": "",
    "excluded_employers": "", "remote_pref": "any", "is_active": "on",
    "preferred_currency": "USD",
}


class _Base(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = self.user.profile
        self.schedule.reset_mock()
        self.client.force_login(self.user)

    def answers(self, **overrides):
        return self.client.post(reverse("profile_answers"), {**ANSWERS_BASE, **overrides})

    def search(self, **overrides):
        return self.client.post(reverse("profile"), {**SEARCH_BASE, **overrides})

    def refreshed(self):
        self.profile.refresh_from_db()
        return self.profile


class AccessTests(_Base):
    def test_anonymous_users_are_sent_to_login(self):
        anon = Client()
        for name, args in (
            ("profile", ()),
            ("profile_answers", ()),
            ("custom_answer_create", ()),
            ("custom_answer_update", (1,)),
            ("custom_answer_delete", (1,)),
        ):
            resp = anon.get(reverse(name, args=args))
            self.assertEqual(resp.status_code, 302, name)
            self.assertIn(reverse("login"), resp["Location"], name)

    def test_the_old_saved_answers_url_redirects_to_the_new_page(self):
        resp = self.client.get(reverse("explicit_answers"))
        self.assertRedirects(resp, reverse("profile_answers"), fetch_redirect_response=False)

    def test_post_endpoints_enforce_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        for name, args in (
            ("profile_answers", ()),
            ("custom_answer_create", ()),
        ):
            self.assertEqual(strict.post(reverse(name, args=args), {}).status_code, 403, name)

    def test_custom_answer_endpoints_are_post_only(self):
        row = write_answer(self.profile, "Pick a color", "blue", "user").row
        self.assertEqual(self.client.get(reverse("custom_answer_create")).status_code, 405)
        self.assertEqual(self.client.get(reverse("custom_answer_update", args=[row.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("custom_answer_delete", args=[row.pk])).status_code, 405)


class TabTests(_Base):
    def test_both_tabs_render_with_the_other_linked(self):
        search = self.client.get(reverse("profile"))
        answers = self.client.get(reverse("profile_answers"))
        self.assertContains(search, 'aria-current="page">Profile</a>')
        self.assertContains(search, reverse("profile_answers"))
        self.assertContains(answers, 'aria-current="page">Auto-apply answers</a>')
        self.assertContains(answers, reverse("profile"))

    def test_search_tab_has_search_fields_only(self):
        resp = self.client.get(reverse("profile"))
        self.assertContains(resp, 'name="target_titles"')
        self.assertContains(resp, 'name="resume"')
        for name in ("phone", "location_country", "visa_country[]", "citizenship_countries", "salary_region_US"):
            self.assertNotContains(resp, f'name="{name}"', msg_prefix=name)

    def test_answers_tab_has_every_section(self):
        resp = self.client.get(reverse("profile_answers"))
        for anchor in ('id="contact"', 'id="work-auth"', 'id="citizenship"', 'id="salary"',
                       'id="custom"', 'id="legacy"'):
            self.assertContains(resp, anchor)
        for name in ("phone", "location_city", "location_country", "mailing_address",
                     "working_timezone", "citizenship_countries", "salary_region_US", "visa_rows_present"):
            self.assertContains(resp, f'name="{name}"', msg_prefix=name)
        self.assertContains(resp, "Countries whose passports you hold")

    def test_answers_tab_has_no_sponsorship_prefill(self):
        """Issue #126: the old page offered sponsorship answers the model
        deliberately leaves unknown. There is no such control any more."""
        self.profile.visa_status_by_country = {"USA": "h1b"}
        self.profile.save()
        resp = self.client.get(reverse("profile_answers"))
        self.assertNotContains(resp, "yes_h1b")
        self.assertNotContains(resp, "Sponsorship")


class SearchTabTests(_Base):
    def test_save_persists_search_fields_redirects_and_rematches(self):
        resp = self.search(target_tags="python, django", min_salary="120000")
        self.assertRedirects(resp, reverse("recommendations"), fetch_redirect_response=False)
        profile = self.refreshed()
        self.assertEqual(profile.target_tags, ["python", "django"])
        self.assertEqual(profile.min_salary, 120000)
        self.assertEqual(self.schedule.call_count, 1)

    def test_a_search_save_never_touches_answers_fields(self):
        self.profile.phone = "555-0100"
        self.profile.location_country = "IND"
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.citizenship_countries = ["IND"]
        self.profile.salary_by_region = {"US": "75-100k"}
        self.profile.save()
        # Posting those fields to the search tab must neither change nor blank them.
        self.search(
            phone="", location_country="", citizenship_countries="",
            **{"visa_country[]": [], "salary_region_US": ""},
        )
        profile = self.refreshed()
        self.assertEqual(profile.phone, "555-0100")
        self.assertEqual(profile.location_country, "IND")
        self.assertEqual(profile.visa_status_by_country, {"USA": "citizen"})
        self.assertEqual(profile.citizenship_countries, ["IND"])
        self.assertEqual(profile.salary_by_region, {"US": "75-100k"})


class AnswersSaveTests(_Base):
    def test_save_persists_contact_fields_and_never_rematches(self):
        resp = self.answers(
            phone="+91 98765 43210", location_city="Pune", location_country="IND",
            mailing_address="12 MG Road\nPune", working_timezone="Asia/Kolkata",
            current_employer="Acme",
        )
        self.assertRedirects(resp, reverse("profile_answers"), fetch_redirect_response=False)
        profile = self.refreshed()
        self.assertEqual(
            (profile.phone, profile.location_city, profile.location_country,
             profile.working_timezone, profile.current_employer),
            ("+91 98765 43210", "Pune", "IND", "Asia/Kolkata", "Acme"),
        )
        self.assertEqual(profile.mailing_address, "12 MG Road\nPune")
        self.assertEqual(self.schedule.call_count, 0)

    def test_an_answers_save_leaves_matching_fields_alone(self):
        self.profile.target_tags = ["python"]
        self.profile.min_salary = 99
        self.profile.headline = "Engineer"
        self.profile.save()
        self.schedule.reset_mock()
        self.answers(phone="555-0100")
        profile = self.refreshed()
        self.assertEqual((profile.target_tags, profile.min_salary, profile.headline),
                         (["python"], 99, "Engineer"))
        self.assertEqual(self.schedule.call_count, 0)

    def test_success_message_is_shown(self):
        resp = self.client.post(reverse("profile_answers"), {**ANSWERS_BASE}, follow=True)
        self.assertContains(resp, "Your answers were saved.")

    def test_visa_rows_round_trip_and_render_server_side(self):
        self.answers(**{"visa_country[]": ["USA", "IND"], "visa_status[]": ["h1b", "citizen"]})
        self.assertEqual(self.refreshed().visa_status_by_country, {"USA": "h1b", "IND": "citizen"})
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, '<option value="USA" selected>')
        self.assertContains(page, '<option value="h1b" selected>')
        self.assertContains(page, '<option value="IND" selected>')

    def test_a_duplicate_country_is_an_error_and_saves_nothing(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        resp = self.answers(
            phone="555", **{"visa_country[]": ["USA", "USA"], "visa_status[]": ["h1b", "opt"]}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "listed more than once")
        profile = self.refreshed()
        self.assertEqual(profile.visa_status_by_country, {"USA": "citizen"})
        self.assertEqual(profile.phone, "")

    def test_an_unknown_country_or_status_is_an_error(self):
        resp = self.answers(**{"visa_country[]": ["Atlantis"], "visa_status[]": ["h1b"]})
        self.assertContains(resp, "not a recognized country")
        resp = self.answers(**{"visa_country[]": ["USA"], "visa_status[]": ["wizard"]})
        self.assertContains(resp, "not a valid status")

    def test_an_invalid_post_re_renders_with_the_users_input_kept(self):
        """Issue #127: the old page dropped the bound form on a bad POST."""
        resp = self.answers(
            phone="555-0100", location_city="Pune",
            **{"visa_country[]": ["USA", "USA"], "visa_status[]": ["h1b", "opt"]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="555-0100"')
        self.assertContains(resp, 'value="Pune"')
        self.assertContains(resp, "listed more than once")
        # and the user's two rows are still on screen
        self.assertContains(resp, '<option value="h1b" selected>')
        self.assertContains(resp, '<option value="opt" selected>')

    def test_a_post_without_the_repeater_marker_never_wipes_stored_visa_rows(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        data = {k: v for k, v in ANSWERS_BASE.items() if k != "visa_rows_present"}
        self.client.post(reverse("profile_answers"), data)
        self.assertEqual(self.refreshed().visa_status_by_country, {"USA": "citizen"})

    def test_a_post_with_the_marker_and_no_rows_clears_them(self):
        self.profile.visa_status_by_country = {"USA": "citizen"}
        self.profile.save()
        self.answers()
        self.assertEqual(self.refreshed().visa_status_by_country, {})

    def test_citizenship_and_salary_round_trip(self):
        self.answers(
            citizenship_countries=["IND", "", "USA", "IND"],  # a blank row and a repeat are dropped
            salary_region_US="75-100k", salary_region_IN="",
        )
        profile = self.refreshed()
        self.assertEqual(profile.citizenship_countries, ["IND", "USA"])
        self.assertEqual(profile.salary_by_region, {"US": "75-100k"})
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, '<option value="75-100k" selected>')

    def test_citizenship_is_one_labelled_row_per_passport_not_a_multi_select(self):
        self.profile.citizenship_countries = ["IND", "USA"]
        self.profile.save()
        page = self.client.get(reverse("profile_answers"))
        html = page.content.decode()
        self.assertNotIn("multiple", html.split('id="citizenship"')[1].split('id="salary"')[0])
        self.assertContains(page, '<option value="IND" selected>')
        self.assertContains(page, '<option value="USA" selected>')
        self.assertContains(page, "Add another passport")
        self.assertContains(page, "Countries whose passports you hold")
        # two saved rows plus one blank row to add a third without JavaScript
        self.assertEqual(html.count('class="citizenship-row"'), 3 + 1)  # +1 is the JS template

    def test_a_post_without_the_citizenship_marker_never_wipes_stored_citizenship(self):
        self.profile.citizenship_countries = ["IND"]
        self.profile.save()
        data = {k: v for k, v in ANSWERS_BASE.items() if k != "citizenship_rows_present"}
        self.client.post(reverse("profile_answers"), data)
        self.assertEqual(self.refreshed().citizenship_countries, ["IND"])

    def test_removing_every_row_with_the_marker_clears_citizenship(self):
        self.profile.citizenship_countries = ["IND"]
        self.profile.save()
        self.answers(citizenship_countries=[""])
        self.assertEqual(self.refreshed().citizenship_countries, [])

    def test_an_unknown_citizenship_country_is_rejected_and_rows_are_kept(self):
        resp = self.answers(citizenship_countries=["IND", "ZZZ"], phone="555")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.refreshed().phone, "")
        self.assertContains(resp, '<option value="IND" selected>')

    def test_per_field_provenance_is_stamped(self):
        self.answers(phone="555-0100", location_country="IND", salary_region_US="75-100k")
        provenance = self.refreshed().field_provenance
        for key in ("phone", "location_country", "salary_by_region.US"):
            self.assertEqual(provenance[key]["source"], "user", key)

    def test_an_invalid_country_or_timezone_choice_is_rejected(self):
        resp = self.answers(location_country="ZZZ", working_timezone="Mars/Olympus")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.refreshed().location_country, "")

    def test_mailing_address_is_capped_at_500_characters(self):
        resp = self.answers(mailing_address="x" * 501)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.refreshed().mailing_address, "")

    def test_a_save_invalidates_the_questions_panel_cache(self):
        key = panel_cache_key(self.user.pk)
        cache.set(key, {"stale": True}, 3600)
        with self.captureOnCommitCallbacks(execute=True):
            self.answers(phone="555-0100")
        self.assertIsNone(cache.get(key))


class CustomAnswerTests(_Base):
    def create(self, **overrides):
        data = {"question_text": "How did you hear about us?", "value": "LinkedIn",
                "category": "other", "scope": "", "min_tier": ""}
        data.update(overrides)
        return self.client.post(reverse("custom_answer_create"), data)

    def test_create_writes_a_user_row_and_shows_it_with_its_source(self):
        resp = self.create()
        self.assertRedirects(
            resp, reverse("profile_answers") + "#custom", fetch_redirect_response=False
        )
        row = AnswerBank.objects.get()
        self.assertEqual((row.value, row.source, row.is_locked, row.scope_region),
                         ("LinkedIn", "user", False, ""))
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, "Auto-apply will use: <strong>LinkedIn</strong>")
        self.assertContains(page, "source: user")

    def test_create_with_lock_and_category(self):
        self.create(lock="on", category="experience", question_text="Years of Go experience?")
        row = AnswerBank.objects.get()
        self.assertEqual((row.is_locked, row.category), (True, "experience"))

    def test_the_tier_can_be_raised_but_never_lowered(self):
        self.create(min_tier="t1_commercial")
        self.assertEqual(AnswerBank.objects.get().risk_tier, "t1_commercial")
        self.create(question_text="Will you require sponsorship for a visa?", min_tier="t1_commercial")
        sponsor = AnswerBank.objects.get(question_text="Will you require sponsorship for a visa?")
        self.assertEqual(sponsor.risk_tier, "t0_legal")

    def test_a_location_sensitive_answer_is_region_scoped_when_a_region_is_chosen(self):
        self.create(question_text="Are you willing to relocate?", value="Yes", scope="US")
        row = AnswerBank.objects.get()
        self.assertEqual(row.scope_region, "US")
        self.assertFalse(row.source_detail.get("applies_everywhere"))

    def test_a_location_sensitive_answer_with_no_region_is_marked_everywhere(self):
        self.create(question_text="Are you willing to relocate?", value="Yes", scope="")
        row = AnswerBank.objects.get()
        self.assertEqual(row.scope_region, "")
        self.assertTrue(row.source_detail["applies_everywhere"])

    def test_invalid_input_re_renders_the_page_with_errors(self):
        resp = self.create(question_text="", value="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This field is required")
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_blank_answer_is_rejected(self):
        resp = self.create(value="   ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_recreating_the_same_question_replaces_the_value_and_keeps_history(self):
        self.create(value="LinkedIn")
        self.create(value="A friend")
        self.assertEqual(AnswerBank.objects.get().value, "A friend")
        self.assertEqual(AnswerBankHistory.objects.get().value, "LinkedIn")

    def test_update_changes_the_value_lock_and_category_of_that_row_only(self):
        self.create()
        other = write_answer(self.profile, "Another question here", "x", "user").row
        row = AnswerBank.objects.get(question_text="How did you hear about us?")
        resp = self.client.post(
            reverse("custom_answer_update", args=[row.pk]),
            {"value": "Twitter", "category": "projects", "lock": "on", "min_tier": ""},
        )
        self.assertRedirects(resp, reverse("profile_answers") + "#custom", fetch_redirect_response=False)
        row.refresh_from_db()
        self.assertEqual((row.value, row.category, row.is_locked), ("Twitter", "projects", True))
        other.refresh_from_db()
        self.assertEqual(other.value, "x")
        self.assertEqual(AnswerBank.objects.count(), 2)  # the edit did not fork a new row

    def test_unticking_lock_unlocks(self):
        self.create(lock="on")
        row = AnswerBank.objects.get()
        self.client.post(
            reverse("custom_answer_update", args=[row.pk]),
            {"value": "LinkedIn", "category": "other"},
        )
        row.refresh_from_db()
        self.assertFalse(row.is_locked)

    def test_update_keeps_a_rows_region_and_everywhere_flag(self):
        self.create(question_text="Are you willing to relocate?", value="Yes", scope="US")
        row = AnswerBank.objects.get()
        self.client.post(
            reverse("custom_answer_update", args=[row.pk]),
            {"value": "No", "category": "relocation", "scope": "IN"},  # scope in the POST is ignored
        )
        row.refresh_from_db()
        self.assertEqual((row.value, row.scope_region), ("No", "US"))
        self.assertEqual(AnswerBank.objects.count(), 1)

    def test_the_question_text_cannot_be_changed_by_an_update(self):
        self.create()
        row = AnswerBank.objects.get()
        self.client.post(
            reverse("custom_answer_update", args=[row.pk]),
            {"question_text": "Something else entirely", "value": "x", "category": "other"},
        )
        row.refresh_from_db()
        self.assertEqual(row.question_text, "How did you hear about us?")

    def test_delete_removes_the_row_and_records_history(self):
        self.create()
        row = AnswerBank.objects.get()
        resp = self.client.post(reverse("custom_answer_delete", args=[row.pk]))
        self.assertRedirects(resp, reverse("profile_answers") + "#custom", fetch_redirect_response=False)
        self.assertEqual(AnswerBank.objects.count(), 0)
        entry = AnswerBankHistory.objects.get()
        self.assertEqual((entry.value, entry.superseded_by_source), ("LinkedIn", "user_delete"))

    def test_a_locked_row_can_still_be_deleted_by_its_owner(self):
        self.create(lock="on")
        self.client.post(reverse("custom_answer_delete", args=[AnswerBank.objects.get().pk]))
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_another_users_rows_are_404(self):
        bob = User.objects.create_user(username="bob", password="pw").profile
        row = write_answer(bob, "Bob's question", "secret", "user").row
        for name in ("custom_answer_update", "custom_answer_delete"):
            resp = self.client.post(reverse(name, args=[row.pk]), {"value": "x", "category": "other"})
            self.assertEqual(resp.status_code, 404, name)
        row.refresh_from_db()
        self.assertEqual(row.value, "secret")
        self.assertNotContains(self.client.get(reverse("profile_answers")), "Bob's question")

    def test_writes_invalidate_the_questions_panel_cache(self):
        key = panel_cache_key(self.user.pk)
        for action in ("create", "delete"):
            cache.set(key, {"stale": True}, 3600)
            with self.captureOnCommitCallbacks(execute=True):
                if action == "create":
                    self.create()
                else:
                    self.client.post(reverse("custom_answer_delete", args=[AnswerBank.objects.get().pk]))
            self.assertIsNone(cache.get(key), action)


class LegacyAnswerTests(_Base):
    def legacy(self, key, value, **detail):
        return AnswerBank.objects.create(
            profile=self.profile, question_key=key, question_text="(saved answer)", value=value,
            risk_tier="t0_legal", source="user", is_locked=True,
            source_detail={"origin": "legacy_explicit_answer", **detail},
        )

    def test_legacy_rows_are_listed_apart_from_custom_answers(self):
        self.legacy("legacy:sponsorship", "No, I do not require sponsorship", answer_bool=False)
        write_answer(self.profile, "A normal question", "yes", "user")
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, "No, I do not require sponsorship")
        self.assertContains(page, "A normal question")
        self.assertEqual(page.context["custom_count"], 1)
        self.assertEqual(len(page.context["legacy_rows"]), 1)

    def test_legacy_rows_cannot_be_edited_only_deleted(self):
        row = self.legacy("legacy:sponsorship", "No", answer_bool=False)
        resp = self.client.post(
            reverse("custom_answer_update", args=[row.pk]), {"value": "Yes", "category": "other"},
            follow=True,
        )
        self.assertContains(resp, "can only be deleted")
        row.refresh_from_db()
        self.assertEqual(row.value, "No")
        resp = self.client.post(reverse("custom_answer_delete", args=[row.pk]))
        self.assertRedirects(resp, reverse("profile_answers") + "#legacy", fetch_redirect_response=False)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_a_conflict_with_the_per_country_settings_is_called_out(self):
        self.legacy("legacy:sponsorship", "No, I do not require sponsorship", answer_bool=False)
        self.profile.visa_status_by_country = {"USA": "requires_sponsorship"}
        self.profile.save()
        page = self.client.get(reverse("profile_answers"))
        self.assertContains(page, "disagree with your per-country settings")
        self.assertContains(page, "United States")

    def test_no_conflict_banner_when_they_agree(self):
        self.legacy("legacy:sponsorship", "Yes, H-1B", answer_bool=True)
        self.profile.visa_status_by_country = {"USA": "requires_sponsorship"}
        self.profile.save()
        self.assertNotContains(self.client.get(reverse("profile_answers")), "disagree with")

    def test_a_region_bound_salary_row_shows_its_region(self):
        self.legacy("legacy:salary_expectation", "$100,000 – $125,000", region="US")
        self.assertContains(self.client.get(reverse("profile_answers")), "US only")
