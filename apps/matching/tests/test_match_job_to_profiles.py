from unittest import mock

from django.test import TestCase

from apps.accounts.models import Profile
from apps.jobs.models import Job
from apps.matching.constants import MatchStatus
from apps.matching.models import UserJobMatch
from apps.matching.scoring import score_job
from apps.matching.services import match_job

from .factories import make_employer, make_job, make_profile


class MatchJobTests(TestCase):
    def setUp(self):
        # Suppress the profile post-save signal's real enqueue during setup.
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.employer = make_employer()

    def test_fanout_happy_path_only_prefilter_passers_get_rows(self):
        job = make_job(self.employer, tags=["python", "kubernetes"], is_remote=False)
        strong = make_profile("strong", tags=["python", "kubernetes"],
                              titles=["Backend Engineer"])
        weak = make_profile("weak", tags=["rust"])
        remote_only = make_profile(
            "remoteonly", tags=["python"],
            remote_pref=Profile.RemotePref.REMOTE_ONLY,
        )  # excluded: job is onsite

        match_job(job)

        self.assertFalse(
            UserJobMatch.objects.filter(user=remote_only.user, job=job).exists()
        )
        strong_match = UserJobMatch.objects.get(user=strong.user, job=job)
        weak_match = UserJobMatch.objects.get(user=weak.user, job=job)
        self.assertEqual(strong_match.match_status, MatchStatus.RECOMMENDED)
        self.assertGreater(strong_match.match_score, weak_match.match_score)
        self.assertEqual(strong_match.matched_tags, ["kubernetes", "python"])

    def test_onsite_only_match_reflects_corrected_location_scoring_end_to_end(self):
        # Integration coverage for the ONSITE_ONLY fix: the corrected
        # location component must actually flow through match_job into a
        # persisted UserJobMatch, not just the pure scorer in isolation.
        us_job = make_job(self.employer, source_job_id="us", tags=["python"],
                           is_remote=False, location="Austin, TX, US",
                           location_city="Austin", location_region="TX",
                           location_country="US", location_resolved=True)
        uk_job = make_job(self.employer, source_job_id="uk", tags=["python"],
                           is_remote=False, location="London, UK",
                           location_city="London", location_country="UK",
                           location_resolved=True)
        profile = make_profile(
            "onsiteonly", tags=["python"],
            remote_pref=Profile.RemotePref.ONSITE_ONLY,
            locations=["US"],
            locations_normalized=[
                {"raw": "US", "city": None, "region": None, "country": "US", "resolved": True}
            ],
        )

        match_job(us_job)
        match_job(uk_job)

        us_match = UserJobMatch.objects.get(user=profile.user, job=us_job)
        uk_match = UserJobMatch.objects.get(user=profile.user, job=uk_job)
        self.assertGreater(us_match.match_score, uk_match.match_score)

    def test_idempotent_rerun_updates_not_duplicates(self):
        job = make_job(self.employer, tags=["python"])
        make_profile("p", tags=["python"])
        match_job(job)
        match_job(job)
        self.assertEqual(UserJobMatch.objects.filter(job=job).count(), 1)

    def test_reclassification_updates_existing_rows_in_place(self):
        job = make_job(self.employer, tags=["python"])
        p = make_profile("p", tags=["python", "kubernetes"])
        match_job(job)
        before = UserJobMatch.objects.get(user=p.user, job=job)

        # Job gains a tag -> higher overlap -> higher score, same row.
        job.classification_tags = ["python", "kubernetes"]
        job.save()
        match_job(job)
        after = UserJobMatch.objects.get(user=p.user, job=job)
        self.assertEqual(before.pk, after.pk)
        self.assertGreater(after.match_score, before.match_score)
        self.assertEqual(after.matched_tags, ["kubernetes", "python"])

    def test_inactive_profiles_never_get_rows(self):
        job = make_job(self.employer, tags=["python"])
        inactive = make_profile("inactive", tags=["python"], is_active=False)
        match_job(job)
        self.assertFalse(
            UserJobMatch.objects.filter(user=inactive.user, job=job).exists()
        )

    def test_prefilter_limits_scoring_invocations(self):
        job = make_job(self.employer, tags=["python"], is_remote=False)
        make_profile("a", tags=["python"])
        make_profile("b", tags=["python"])
        make_profile("c", tags=["python"], remote_pref=Profile.RemotePref.REMOTE_ONLY)

        with mock.patch(
            "apps.matching.services.score_job", wraps=score_job
        ) as spy:
            match_job(job)
        # Only the 2 candidates are scored — the remote-only profile is excluded
        # by the DB pre-filter and never reaches the scorer.
        self.assertEqual(spy.call_count, 2)

    def test_excluded_employer_profile_gets_no_row(self):
        job = make_job(self.employer, tags=["python"])
        p = make_profile("p", tags=["python"], excluded=["acme"])
        match_job(job)
        self.assertFalse(UserJobMatch.objects.filter(user=p.user, job=job).exists())

    def test_closed_job_removes_existing_matches(self):
        job = make_job(self.employer, tags=["python"])
        make_profile("p", tags=["python"])
        match_job(job)
        self.assertEqual(UserJobMatch.objects.filter(job=job).count(), 1)

        job.status = Job.Status.CLOSED
        job.save()
        stats = match_job(job)
        self.assertTrue(stats["closed"])
        self.assertEqual(UserJobMatch.objects.filter(job=job).count(), 0)

    def test_job_flip_to_onsite_removes_stale_remote_only_match(self):
        job = make_job(self.employer, tags=["python"], is_remote=True)
        p = make_profile("p", tags=["python"], remote_pref=Profile.RemotePref.REMOTE_ONLY)
        match_job(job)
        self.assertTrue(UserJobMatch.objects.filter(user=p.user, job=job).exists())

        # Job becomes onsite; the remote-only user is no longer a candidate.
        job.is_remote = False
        job.save()
        match_job(job)
        self.assertFalse(UserJobMatch.objects.filter(user=p.user, job=job).exists())

    def test_zero_tag_job_matches_without_crashing(self):
        job = make_job(self.employer, tags=[])
        make_profile("p", tags=["python"])
        stats = match_job(job)  # scores everyone; nobody has tag overlap
        self.assertEqual(stats["scored"], 1)
