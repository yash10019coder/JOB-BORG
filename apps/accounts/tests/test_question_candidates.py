"""Candidate retrieval for reworded questions (Phase 4b, #130): read-only, own rows only."""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import AnswerBank
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import load_bank_rows, normalize_question_key, write_answer
from apps.accounts.services.question_candidates import find_candidates

User = get_user_model()

NOTICE = "What is your notice period?"
RELOCATE = "Are you willing to relocate?"


def job(country="US", employer="Acme"):
    return SimpleNamespace(location_country=country, employer=SimpleNamespace(name=employer))


@override_settings(QUESTION_CANDIDATES_ENABLED=True)
class CandidateTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile

    def store(self, text, value, source="user", **kwargs):
        result = write_answer(self.profile, text, value, source, **kwargs)
        self.assertTrue(result.applied)
        return result.row

    def test_a_reworded_question_gets_the_earlier_answer(self):
        self.store(NOTICE, "4 weeks")
        found = find_candidates(self.profile, "Notice period?", options=("2 weeks", "4 weeks"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].question_text, NOTICE)
        self.assertEqual(found[0].value, "4 weeks")
        self.assertEqual(found[0].mapped_value, "4 weeks")
        self.assertEqual(found[0].source, "user")

    def test_a_value_that_does_not_fit_the_live_options_is_text_only(self):
        """QC6."""
        self.store(NOTICE, "4 weeks")
        found = find_candidates(self.profile, "Notice period?", options=("Immediately", "1 month"))
        self.assertEqual(found[0].value, "4 weeks")
        self.assertIsNone(found[0].mapped_value)

    def test_free_text_fields_keep_the_value(self):
        self.store(NOTICE, "4 weeks")
        self.assertEqual(find_candidates(self.profile, "Notice period?")[0].mapped_value, "4 weeks")

    def test_nothing_known_returns_nothing(self):
        self.assertEqual(find_candidates(self.profile, "Notice period?"), [])

    def test_missing_profile_blank_question_and_off_setting_return_nothing(self):
        self.store(NOTICE, "4 weeks")
        self.assertEqual(find_candidates(None, "Notice period?"), [])
        self.assertEqual(find_candidates(self.profile, "  "), [])
        with override_settings(QUESTION_CANDIDATES_ENABLED=False):
            self.assertEqual(find_candidates(self.profile, "Notice period?"), [])

    def test_the_exact_question_is_not_a_candidate(self):
        """QC2."""
        self.store(NOTICE, "4 weeks")
        self.assertEqual(find_candidates(self.profile, "what is your notice period"), [])

    def test_expired_legacy_and_textless_rows_are_skipped(self):
        """QC2."""
        row = self.store(NOTICE, "4 weeks")
        AnswerBank.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
        AnswerBank.objects.create(
            profile=self.profile, question_key="legacy:notice", question_text=NOTICE, value="x",
            risk_tier="t1_commercial", source="user",
        )
        AnswerBank.objects.create(
            profile=self.profile, question_key="start date", question_text="", value="x",
            risk_tier="t1_commercial", source="user",
        )
        self.assertEqual(find_candidates(self.profile, "Notice period?"), [])

    def test_learned_rows_are_ignored_while_learning_is_off(self):
        """QC3."""
        self.store(NOTICE, "4 weeks", source="learned")
        self.assertEqual(len(find_candidates(self.profile, "Notice period?")), 1)
        self.profile.learning_enabled = False
        self.profile.save()
        self.assertEqual(find_candidates(self.profile, "Notice period?"), [])

    def test_the_limit_caps_results_and_user_rows_rank_above_learned(self):
        """QC5."""
        self.store("Notice period (weeks)?", "6", source="learned")
        self.store("How much notice do you need to give your current employer?", "4")
        self.store("What is your notice period at present?", "2", source="imported")
        found = find_candidates(self.profile, NOTICE, limit=2)
        self.assertEqual(len(found), 2)
        self.assertEqual([c.source for c in found], ["user", "learned"])
        self.assertEqual(len(find_candidates(self.profile, NOTICE, limit=0)), 0)

    def test_a_locked_user_row_outranks_an_unlocked_one(self):
        """QC5."""
        self.store("Notice period?", "1", is_locked=True)
        self.store("How much notice do you need to give?", "2")
        found = find_candidates(self.profile, NOTICE)
        self.assertTrue(found[0].is_locked)

    def test_guards_from_the_matcher_apply_end_to_end(self):
        self.store("Are you NOT authorized to work in the US?", "Yes")
        self.assertEqual(find_candidates(self.profile, "Are you authorized to work in the US?"), [])
        self.store("Do you have a UK passport?", "Yes")
        self.assertEqual(find_candidates(self.profile, "Do you have a US passport?"), [])

    def test_typed_owned_questions_get_no_candidates(self):
        self.store("What is your citizenship?", "Canadian")
        self.assertEqual(find_candidates(self.profile, "Which country are you a citizen of?"), [])

    def test_sponsorship_never_pairs_with_authorization(self):
        self.store("Will you require sponsorship in the US?", "No")
        self.assertEqual(find_candidates(self.profile, "Are you authorized to work in the US?"), [])


@override_settings(QUESTION_CANDIDATES_ENABLED=True)
class IsolationTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="pw").profile
        self.bob = User.objects.create_user(username="bob", password="pw").profile

    def test_another_profiles_rows_are_never_returned(self):
        """QC1."""
        write_answer(self.bob, NOTICE, "4 weeks", "user")
        self.assertEqual(find_candidates(self.alice, "Notice period?"), [])
        self.assertEqual(len(find_candidates(self.bob, "Notice period?")), 1)

    def test_a_mixed_preload_still_returns_only_the_askers_rows(self):
        """QC1."""
        write_answer(self.alice, "Notice period (weeks)?", "6", "user")
        write_answer(self.bob, NOTICE, "4 weeks", "user")
        mixed = {**load_bank_rows(self.alice), **load_bank_rows(self.bob)}
        found = find_candidates(self.alice, "How much notice do you need to give?", bank_rows=mixed)
        self.assertEqual([c.value for c in found], ["6"])


@override_settings(QUESTION_CANDIDATES_ENABLED=True)
class RegionTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.us, self.india = job("US"), job("India")
        self.us_region = typed_facts.job_region(self.us)
        self.in_region = typed_facts.job_region(self.india)
        self.assertNotEqual(self.us_region, self.in_region)
        self.assertTrue(self.us_region and self.in_region)

    def test_a_relocation_answer_saved_for_one_region_is_not_offered_in_another(self):
        """QC4."""
        write_answer(self.profile, RELOCATE, "Yes", "user", scope_region=self.us_region)
        reworded = "Would you be open to relocation?"
        self.assertEqual(len(find_candidates(self.profile, reworded, self.us)), 1)
        self.assertEqual(find_candidates(self.profile, reworded, self.india), [])

    def test_the_same_row_marked_applies_everywhere_is_offered_anywhere(self):
        write_answer(self.profile, RELOCATE, "Yes", "user", scope_region=self.us_region, applies_everywhere=True)
        self.assertEqual(len(find_candidates(self.profile, "Would you be open to relocation?", self.india)), 1)

    def test_a_global_row_for_a_location_question_needs_applies_everywhere(self):
        write_answer(self.profile, RELOCATE, "Yes", "user")
        self.assertEqual(find_candidates(self.profile, "Would you be open to relocation?", self.us), [])

    def test_no_job_means_no_region_only_applies_everywhere_rows(self):
        write_answer(self.profile, RELOCATE, "Yes", "user", scope_region=self.us_region)
        self.assertEqual(find_candidates(self.profile, "Would you be open to relocation?"), [])


@override_settings(QUESTION_CANDIDATES_ENABLED=True)
class ReadOnlyTests(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        write_answer(self.profile, NOTICE, "4 weeks", "user")

    def test_no_query_beyond_the_preload_and_nothing_is_written(self):
        """QC7."""
        rows = load_bank_rows(self.profile)
        before = list(AnswerBank.objects.values_list("pk", "value", "updated_at", "source"))
        with self.assertNumQueries(0):
            found = find_candidates(self.profile, "Notice period?", bank_rows=rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(before, list(AnswerBank.objects.values_list("pk", "value", "updated_at", "source")))

    def test_the_exact_key_helper_agrees_with_the_resolver(self):
        self.assertEqual(
            normalize_question_key(NOTICE), AnswerBank.objects.get(profile=self.profile).question_key
        )
