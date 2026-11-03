from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import AnswerBank, AnswerBankHistory, AnswerObservation, ProfileSuggestion
from apps.accounts.services.answer_resolver import normalize_question_key, write_answer
from apps.accounts.services.learning import fingerprint, learn_for_profile
from apps.accounts.services.panel_cache import panel_cache_key

User = get_user_model()

HEARD = "How did you hear about us?"  # T2
NOTICE = "What is your notice period?"  # T1


def _suggestion(profile, text, value, **extra):
    return ProfileSuggestion.objects.create(
        profile=profile, question_key=normalize_question_key(text), question_text=text,
        value=value, value_fingerprint=fingerprint(value), tier="t1_commercial",
        evidence={"employers": ["Acme", "Beta"], "count": 2}, **extra,
    )


class _Base(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = self.user.profile
        self.other = User.objects.create_user(username="bob", password="pw").profile
        self.schedule.reset_mock()
        self.client.force_login(self.user)

    def learned(self, text, value, profile=None):
        return write_answer(profile or self.profile, text, value, "learned").row

    def row(self, text):
        return AnswerBank.objects.filter(
            profile=self.profile, question_key=normalize_question_key(text)
        ).first()


class AccessTests(_Base):
    def test_every_learning_url_requires_login(self):
        row = self.learned(HEARD, "x")
        suggestion = _suggestion(self.profile, NOTICE, "2 weeks")
        anon = Client()
        urls = [
            reverse("profile_learning"),
            reverse("learning_settings"),
            reverse("learning_run"),
            reverse("learned_forget_all"),
            reverse("learned_promote", args=[row.pk]),
            reverse("learned_forget", args=[row.pk]),
            reverse("suggestion_accept", args=[suggestion.pk]),
            reverse("suggestion_dismiss", args=[suggestion.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = anon.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_write_endpoints_reject_get(self):
        row = self.learned(HEARD, "x")
        for name, args in (("learning_run", []), ("learned_promote", [row.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 405)


class PageTests(_Base):
    def test_the_tab_is_linked_from_the_other_profile_pages(self):
        for name in ("profile", "profile_answers"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(reverse("profile_learning"), html)

    def test_empty_state_explains_how_learning_works(self):
        html = self.client.get(reverse("profile_learning")).content.decode()
        self.assertIn("Nothing learned yet", html)
        self.assertIn("No suggestions right now", html)

    def test_lists_only_the_users_own_rows_and_suggestions(self):
        self.learned(HEARD, "Referral")
        self.learned(HEARD, "Secret", profile=self.other)
        _suggestion(self.profile, NOTICE, "2 weeks")
        _suggestion(self.other, NOTICE, "Never")
        html = self.client.get(reverse("profile_learning")).content.decode()
        self.assertIn("Referral", html)
        self.assertIn("2 weeks", html)
        self.assertIn("Acme, Beta", html)
        self.assertNotIn("Secret", html)
        self.assertNotIn("Never", html)

    def test_user_answers_are_not_listed_as_learned(self):
        write_answer(self.profile, HEARD, "Mine", "user")
        html = self.client.get(reverse("profile_learning")).content.decode()
        self.assertNotIn("Mine", html)

    def test_the_answers_page_hides_learned_rows_and_links_to_the_tab(self):
        self.learned(HEARD, "Referral")
        write_answer(self.profile, "Favourite editor?", "vim", "user")
        html = self.client.get(reverse("profile_answers")).content.decode()
        self.assertNotIn("Referral", html)
        self.assertIn("vim", html)
        self.assertIn("1 answer learned from your applications", html)


class PromoteAndForgetTests(_Base):
    def test_promoting_a_t2_learned_answer_makes_it_the_users_unlocked_answer(self):
        learned = self.learned(HEARD, "Referral")
        self.client.post(reverse("learned_promote", args=[learned.pk]))
        row = self.row(HEARD)
        self.assertEqual((row.value, row.source, row.is_locked), ("Referral", "user", False))
        self.assertEqual(row.source_detail["origin"], "learning_accept")
        self.assertTrue(AnswerBankHistory.objects.filter(source="learned").exists())

    def test_promoting_a_t1_learned_answer_locks_it_and_accepts_its_suggestion(self):
        learned = self.learned(NOTICE, "2 weeks")
        suggestion = _suggestion(self.profile, NOTICE, "2 weeks")
        self.client.post(reverse("learned_promote", args=[learned.pk]))
        row = self.row(NOTICE)
        self.assertEqual((row.source, row.is_locked), ("user", True))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, "accepted")

    def test_forgetting_deletes_the_row_records_suppression_and_blocks_relearning(self):
        for employer in ("Acme", "Beta"):
            AnswerObservation.objects.create(
                profile=self.profile, question_key=normalize_question_key(HEARD),
                question_text=HEARD, value="Referral", tier="t2_factual", field_type="text",
                user_edited=True, job_id=AnswerObservation.objects.count() + 1,
                employer_name=employer, job_region="US",
            )
        learn_for_profile(self.profile)
        learned = self.row(HEARD)
        self.client.post(reverse("learned_forget", args=[learned.pk]))
        self.assertIsNone(self.row(HEARD))
        self.assertEqual(ProfileSuggestion.objects.get().status, "rejected")
        self.assertEqual(AnswerBankHistory.objects.get().superseded_by_source, "user_forget")
        learn_for_profile(self.profile)
        self.assertIsNone(self.row(HEARD))  # same value is not re-proposed

    def test_forget_all(self):
        self.learned(HEARD, "a")
        self.learned(NOTICE, "b")
        write_answer(self.profile, "Mine?", "keep", "user")
        self.client.post(reverse("learned_forget_all"))
        self.assertEqual(AnswerBank.objects.filter(source="learned").count(), 0)
        self.assertEqual(AnswerBank.objects.filter(source="user").count(), 1)
        self.assertEqual(ProfileSuggestion.objects.filter(status="rejected").count(), 2)

    def test_writes_invalidate_the_questions_panel_cache(self):
        learned = self.learned(HEARD, "x")
        cache.set(panel_cache_key(self.user.pk), {"stale": True})
        with self.captureOnCommitCallbacks(execute=True):  # invalidation runs on commit
            self.client.post(reverse("learned_forget", args=[learned.pk]))
        self.assertIsNone(cache.get(panel_cache_key(self.user.pk)))

    def test_other_users_rows_are_a_404(self):
        theirs = self.learned(HEARD, "x", profile=self.other)
        for name in ("learned_promote", "learned_forget"):
            self.assertEqual(self.client.post(reverse(name, args=[theirs.pk])).status_code, 404)
        self.assertTrue(AnswerBank.objects.filter(pk=theirs.pk).exists())

    def test_a_user_row_cannot_be_forgotten_through_the_learning_urls(self):
        mine = write_answer(self.profile, HEARD, "x", "user").row
        self.assertEqual(self.client.post(reverse("learned_forget", args=[mine.pk])).status_code, 404)
        self.assertTrue(AnswerBank.objects.filter(pk=mine.pk).exists())


class SuggestionTests(_Base):
    def test_accepting_writes_a_locked_user_answer(self):
        suggestion = _suggestion(self.profile, NOTICE, "2 weeks")
        self.client.post(reverse("suggestion_accept", args=[suggestion.pk]))
        row = self.row(NOTICE)
        self.assertEqual((row.value, row.source, row.is_locked), ("2 weeks", "user", True))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, "accepted")
        self.assertIsNotNone(suggestion.resolved_at)

    def test_dismissing_rejects_it_and_the_learner_does_not_bring_it_back(self):
        suggestion = _suggestion(self.profile, NOTICE, "2 weeks")
        self.client.post(reverse("suggestion_dismiss", args=[suggestion.pk]))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, "rejected")
        for employer in ("Acme", "Beta"):
            AnswerObservation.objects.create(
                profile=self.profile, question_key=normalize_question_key(NOTICE),
                question_text=NOTICE, value="2 weeks", tier="t1_commercial", field_type="text",
                user_edited=True, job_id=AnswerObservation.objects.count() + 1,
                employer_name=employer, job_region="US",
            )
        learn_for_profile(self.profile)
        self.assertIsNone(self.row(NOTICE))
        self.assertEqual(ProfileSuggestion.objects.count(), 1)

    def test_resolved_or_foreign_suggestions_are_a_404(self):
        done = _suggestion(self.profile, NOTICE, "2 weeks", status="accepted")
        theirs = _suggestion(self.other, NOTICE, "Never")
        for suggestion in (done, theirs):
            for name in ("suggestion_accept", "suggestion_dismiss"):
                with self.subTest(name=name, pk=suggestion.pk):
                    self.assertEqual(
                        self.client.post(reverse(name, args=[suggestion.pk])).status_code, 404
                    )
        self.assertIsNone(self.row(NOTICE))


class SettingsAndRunTests(_Base):
    def test_switching_learning_off_saves_the_flag_without_a_rematch(self):
        self.client.post(reverse("learning_settings"), {})  # unchecked box posts nothing
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.learning_enabled)
        self.schedule.assert_not_called()
        self.client.post(reverse("learning_settings"), {"learning_enabled": "on"})
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.learning_enabled)
        self.schedule.assert_not_called()

    def test_the_switch_does_not_touch_other_profile_fields(self):
        self.profile.full_name = "Alice"
        self.profile.save(update_fields=["full_name"])
        self.client.post(reverse("learning_settings"), {})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, "Alice")

    def test_run_now_learns_from_the_users_observations(self):
        for employer in ("Acme", "Beta"):
            AnswerObservation.objects.create(
                profile=self.profile, question_key=normalize_question_key(HEARD),
                question_text=HEARD, value="Referral", tier="t2_factual", field_type="text",
                user_edited=True, job_id=AnswerObservation.objects.count() + 1,
                employer_name=employer, job_region="US",
            )
        response = self.client.post(reverse("learning_run"), follow=True)
        self.assertEqual(self.row(HEARD).source, "learned")
        self.assertContains(response, "Learned 1")

    def test_run_now_with_nothing_to_learn_says_so(self):
        response = self.client.post(reverse("learning_run"), follow=True)
        self.assertContains(response, "Nothing new to learn yet")

    def test_run_is_refused_while_learning_is_off(self):
        self.profile.learning_enabled = False
        self.profile.save(update_fields=["learning_enabled"])
        response = self.client.post(reverse("learning_run"), follow=True)
        self.assertContains(response, "Learning is off")
        html = self.client.get(reverse("profile_learning")).content.decode()
        self.assertIn("disabled", html)
