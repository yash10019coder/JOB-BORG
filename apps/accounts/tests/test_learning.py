import ast
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import AnswerBank, AnswerBankHistory, AnswerObservation, ProfileSuggestion
from apps.accounts.services import learning
from apps.accounts.services.answer_resolver import normalize_question_key, write_answer
from apps.accounts.services.learning import fingerprint, learn_for_profile
from apps.accounts.tiering import Tier

User = get_user_model()

NOTICE = "What is your notice period?"  # T1
GENDER = "What is your gender?"  # T0
HEARD = "How did you hear about us?"  # T2
RELOCATE = "Are you willing to relocate?"  # T1, location-sensitive
CITIZEN = "Are you a citizen of the United States?"  # typed-setting owned


class _Base(TestCase):
    def setUp(self):
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self._job = 0

    def observe(self, text, value, employer, *, edited=True, declined=False, job_id=None,
                field_type="text", tier=Tier.T2_FACTUAL, region="US", profile=None):
        self._job += 1
        return AnswerObservation.objects.create(
            profile=profile or self.profile,
            question_key=normalize_question_key(text),
            question_text=text,
            value=value,
            tier=tier,
            field_type=field_type,
            user_edited=edited,
            remember_declined=declined,
            job_id=self._job if job_id is None else job_id,
            employer_name=employer,
            job_region=region,
        )

    def learned(self, text, scope=""):
        return AnswerBank.objects.filter(
            profile=self.profile, question_key=normalize_question_key(text), scope_region=scope
        ).first()


class ConsensusCountingTests(_Base):
    def test_two_employers_agreeing_write_a_learned_row_with_evidence(self):
        self.observe(HEARD, "LinkedIn", "Acme")
        self.observe(HEARD, "LinkedIn", "Beta")
        report = learn_for_profile(self.profile)
        row = self.learned(HEARD)
        self.assertEqual((row.value, row.source, row.is_locked), ("LinkedIn", "learned", False))
        self.assertEqual(row.source_detail["origin"], "consensus")
        self.assertEqual(row.source_detail["employers"], ["Beta", "Acme"])
        self.assertEqual(row.source_detail["learner_version"], learning.LEARNER_VERSION)
        self.assertEqual(len(report.written), 1)

    def test_one_employer_with_two_jobs_is_not_consensus(self):
        self.observe(HEARD, "LinkedIn", "Acme")
        self.observe(HEARD, "LinkedIn", "ACME ")
        learn_for_profile(self.profile)
        self.assertIsNone(self.learned(HEARD))

    def test_a_single_observation_is_not_consensus(self):
        self.observe(HEARD, "LinkedIn", "Acme")
        report = learn_for_profile(self.profile)
        self.assertIsNone(self.learned(HEARD))
        self.assertEqual(report.skipped["not_enough_evidence"], 1)

    def test_disagreement_writes_nothing(self):
        self.observe(HEARD, "LinkedIn", "Acme")
        self.observe(HEARD, "A friend", "Beta")
        report = learn_for_profile(self.profile)
        self.assertIsNone(self.learned(HEARD))
        self.assertEqual(report.skipped["disagreement"], 1)

    def test_comparison_ignores_case_whitespace_and_list_order(self):
        self.observe(HEARD, "  linkedin ", "Acme")
        self.observe(HEARD, "LinkedIn", "Beta")
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(HEARD).value, "LinkedIn")  # newest submitted form
        self.observe("Which tools?", ["b", "a"], "Acme", field_type="multi_select")
        self.observe("Which tools?", ["A", "B"], "Beta", field_type="multi_select")
        learn_for_profile(self.profile)
        self.assertIsNotNone(self.learned("Which tools?"))

    def test_the_newest_answer_wins_after_it_is_repeated(self):
        for value, employer in (("A", "E1"), ("A", "E2")):
            self.observe(HEARD, value, employer)
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(HEARD).value, "A")
        self.observe(HEARD, "B", "E3")
        learn_for_profile(self.profile)  # newest differs: A is withdrawn
        self.assertIsNone(self.learned(HEARD))
        self.observe(HEARD, "B", "E4")
        learn_for_profile(self.profile)  # B,B: B is learned, replacing nothing
        self.assertEqual(self.learned(HEARD).value, "B")

    def test_a_changed_mind_replaces_the_learned_value_and_keeps_history(self):
        write_answer(self.profile, HEARD, "A", "learned")
        self.observe(HEARD, "B", "E1")
        self.observe(HEARD, "B", "E2")
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(HEARD).value, "B")
        self.assertTrue(AnswerBankHistory.objects.filter(value="A").exists())

    def test_withdrawal_deletes_the_learned_row_with_history(self):
        write_answer(self.profile, HEARD, "A", "learned")
        self.observe(HEARD, "B", "E1")
        report = learn_for_profile(self.profile)
        self.assertIsNone(self.learned(HEARD))
        self.assertEqual(len(report.withdrawn), 1)
        history = AnswerBankHistory.objects.get()
        self.assertEqual((history.value, history.superseded_by_source), ("A", "learner_withdraw"))


class IgnoredEvidenceTests(_Base):
    def assert_not_learned(self, text):
        learn_for_profile(self.profile)
        self.assertIsNone(self.learned(text))

    def pair(self, text, **kwargs):
        self.observe(text, "x", "Acme", **kwargs)
        self.observe(text, "x", "Beta", **kwargs)

    def test_observations_without_a_job_are_ignored(self):
        for employer in ("Acme", "Beta"):
            obs = self.observe(HEARD, "x", employer)
            AnswerObservation.objects.filter(pk=obs.pk).update(job_id=None)
        self.assert_not_learned(HEARD)

    def test_textarea_and_file_answers_are_ignored(self):
        self.pair(HEARD, field_type="textarea")
        self.assert_not_learned(HEARD)
        self.pair("Upload", field_type="file")
        self.assert_not_learned("Upload")

    def test_a_declined_remember_box_is_not_evidence(self):
        self.pair(HEARD, declined=True)
        self.assert_not_learned(HEARD)

    def test_unedited_answers_are_not_evidence(self):
        """An LLM guess re-posted, or a learned prefill the user only confirmed."""
        self.pair(HEARD, edited=False)
        self.assert_not_learned(HEARD)

    def test_a_confirmed_prefill_does_not_reinforce_itself(self):
        write_answer(self.profile, HEARD, "x", "learned")
        self.observe(HEARD, "x", "Acme", edited=False)
        self.observe(HEARD, "x", "Beta", edited=False)
        report = learn_for_profile(self.profile)
        self.assertEqual(self.learned(HEARD).source_detail, {})  # untouched
        self.assertFalse(report.written)

    def test_questions_owned_by_typed_settings_are_skipped(self):
        self.pair(CITIZEN)
        report = learn_for_profile(self.profile)
        self.assertIsNone(self.learned(CITIZEN))
        self.assertEqual(report.skipped["typed_setting_owns_it"], 1)

    def test_other_profiles_observations_do_not_count(self):
        other = User.objects.create_user(username="bob", password="pw").profile
        self.observe(HEARD, "x", "Acme")
        self.observe(HEARD, "x", "Beta", profile=other)
        self.assert_not_learned(HEARD)


class PrecedenceAndSuggestionTests(_Base):
    def test_an_existing_user_answer_is_never_overwritten_or_suggested_over(self):
        for locked in (False, True):
            with self.subTest(locked=locked):
                AnswerBank.objects.all().delete()
                ProfileSuggestion.objects.all().delete()
                write_answer(self.profile, NOTICE, "1 month", "user", is_locked=locked)
                self.observe(NOTICE, "2 weeks", "Acme", tier=Tier.T1_COMMERCIAL)
                self.observe(NOTICE, "2 weeks", "Beta", tier=Tier.T1_COMMERCIAL)
                report = learn_for_profile(self.profile)
                self.assertEqual(self.learned(NOTICE).value, "1 month")
                self.assertEqual(self.learned(NOTICE).source, "user")
                self.assertEqual(ProfileSuggestion.objects.count(), 0)
                self.assertEqual(report.skipped["user_answer_exists"], 1)
                AnswerObservation.objects.all().delete()

    def test_t0_and_t1_get_a_learned_row_and_one_pending_suggestion(self):
        for text, tier in ((NOTICE, Tier.T1_COMMERCIAL), (GENDER, Tier.T0_LEGAL)):
            self.observe(text, "x", "Acme", tier=tier)
            self.observe(text, "x", "Beta", tier=tier)
        learn_for_profile(self.profile)
        learn_for_profile(self.profile)  # rerun: no duplicates
        self.assertEqual(ProfileSuggestion.objects.filter(status="pending").count(), 2)
        suggestion = ProfileSuggestion.objects.get(question_key=normalize_question_key(NOTICE))
        self.assertEqual(suggestion.value, "x")
        self.assertEqual(suggestion.evidence["count"], 2)
        self.assertEqual(suggestion.value_fingerprint, fingerprint("x"))
        self.assertIsNotNone(self.learned(NOTICE))

    def test_t2_gets_no_suggestion(self):
        self.observe(HEARD, "x", "Acme")
        self.observe(HEARD, "x", "Beta")
        learn_for_profile(self.profile)
        self.assertEqual(ProfileSuggestion.objects.count(), 0)

    def test_a_rejected_value_is_not_proposed_again_but_a_new_one_is(self):
        self.observe(NOTICE, "2 weeks", "Acme", tier=Tier.T1_COMMERCIAL)
        self.observe(NOTICE, "2 weeks", "Beta", tier=Tier.T1_COMMERCIAL)
        learn_for_profile(self.profile)
        AnswerBank.objects.all().delete()
        ProfileSuggestion.objects.update(status="rejected")
        report = learn_for_profile(self.profile)
        self.assertIsNone(self.learned(NOTICE))
        self.assertEqual(report.skipped["suppressed_by_user"], 1)
        self.observe(NOTICE, "1 month", "Gamma", tier=Tier.T1_COMMERCIAL)
        self.observe(NOTICE, "1 month", "Delta", tier=Tier.T1_COMMERCIAL)
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(NOTICE).value, "1 month")

    def test_an_expired_suggestion_is_revived_when_the_consensus_returns(self):
        self.observe(NOTICE, "2 weeks", "Acme", tier=Tier.T1_COMMERCIAL)
        self.observe(NOTICE, "2 weeks", "Beta", tier=Tier.T1_COMMERCIAL)
        learn_for_profile(self.profile)
        ProfileSuggestion.objects.update(status="expired")
        AnswerBank.objects.all().delete()
        learn_for_profile(self.profile)
        self.assertEqual(ProfileSuggestion.objects.get().status, "pending")

    def test_a_stale_pending_suggestion_expires(self):
        ProfileSuggestion.objects.create(
            profile=self.profile, question_key="old", value="x",
            value_fingerprint=fingerprint("x"), tier="t1_commercial",
        )
        ProfileSuggestion.objects.update(created_at=timezone.now() - timedelta(days=91))
        report = learn_for_profile(self.profile)
        self.assertEqual(report.expired, 1)
        self.assertEqual(ProfileSuggestion.objects.get().status, "expired")


class ScopeAndModeTests(_Base):
    def test_location_sensitive_questions_are_learned_per_region(self):
        for region in ("US", "IN"):
            self.observe(RELOCATE, "Yes" if region == "US" else "No", f"E-{region}-1",
                         tier=Tier.T1_COMMERCIAL, region=region)
            self.observe(RELOCATE, "Yes" if region == "US" else "No", f"E-{region}-2",
                         tier=Tier.T1_COMMERCIAL, region=region)
        learn_for_profile(self.profile)
        self.assertEqual(self.learned(RELOCATE, "US").value, "Yes")
        self.assertEqual(self.learned(RELOCATE, "IN").value, "No")
        self.assertIsNone(self.learned(RELOCATE, ""))

    def test_location_sensitive_without_a_region_is_skipped(self):
        self.observe(RELOCATE, "Yes", "Acme", tier=Tier.T1_COMMERCIAL, region="")
        self.observe(RELOCATE, "Yes", "Beta", tier=Tier.T1_COMMERCIAL, region="")
        report = learn_for_profile(self.profile)
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.assertEqual(report.skipped["location_sensitive_without_region"], 2)

    def test_learning_off_writes_nothing(self):
        self.observe(HEARD, "x", "Acme")
        self.observe(HEARD, "x", "Beta")
        self.profile.learning_enabled = False
        self.profile.save(update_fields=["learning_enabled"])
        report = learn_for_profile(self.profile)
        self.assertTrue(report.disabled)
        self.assertEqual(AnswerBank.objects.count(), 0)

    def test_dry_run_reports_without_writing(self):
        self.observe(NOTICE, "x", "Acme", tier=Tier.T1_COMMERCIAL)
        self.observe(NOTICE, "x", "Beta", tier=Tier.T1_COMMERCIAL)
        report = learn_for_profile(self.profile, dry_run=True)
        self.assertEqual((len(report.written), len(report.suggested)), (1, 1))
        self.assertEqual(AnswerBank.objects.count(), 0)
        self.assertEqual(ProfileSuggestion.objects.count(), 0)

    def test_rerunning_with_no_new_data_changes_nothing(self):
        self.observe(HEARD, "x", "Acme")
        self.observe(HEARD, "x", "Beta")
        learn_for_profile(self.profile)
        row = self.learned(HEARD)
        report = learn_for_profile(self.profile)
        self.assertFalse(report.changed)
        self.assertEqual(AnswerBankHistory.objects.count(), 0)
        self.assertEqual(self.learned(HEARD).updated_at, row.updated_at)

    def test_the_minimum_is_tunable(self):
        self.observe(HEARD, "x", "Acme")
        self.observe(HEARD, "x", "Beta")
        with self.settings(LEARNING_MIN_DISTINCT_EMPLOYERS=3):
            learn_for_profile(self.profile)
            self.assertIsNone(self.learned(HEARD))
            self.observe(HEARD, "x", "Gamma")
            learn_for_profile(self.profile)
            self.assertIsNotNone(self.learned(HEARD))


class LayeringTests(TestCase):
    def test_the_learner_does_not_import_auto_apply_or_web(self):
        tree = ast.parse(Path(learning.__file__).read_text())
        modules = [
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (getattr(node, "names", [None]) if isinstance(node, ast.Import) else [None])
        ]
        for name in filter(None, modules):
            self.assertFalse(name.startswith(("apps.auto_apply", "apps.web")), name)
