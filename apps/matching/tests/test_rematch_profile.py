from unittest import mock

from django.test import TestCase
from django.utils import timezone
import datetime

from apps.jobs.models import Job
from apps.matching.models import UserJobMatch
from apps.matching.services import match_job, rematch_profile_obj

from .factories import make_employer, make_job, make_profile


class RematchProfileTests(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.employer = make_employer()

    def test_rematch_scores_recent_open_window_only(self):
        recent = make_job(self.employer, "1", tags=["python"])
        old = make_job(
            self.employer, "2", tags=["python"],
            scraped_at=timezone.now() - datetime.timedelta(days=90),
        )
        p = make_profile("p", tags=["python"])

        rematch_profile_obj(p)
        self.assertTrue(UserJobMatch.objects.filter(user=p.user, job=recent).exists())
        # The out-of-window job is not matched.
        self.assertFalse(UserJobMatch.objects.filter(user=p.user, job=old).exists())

    def test_narrowing_deletes_disqualified_match(self):
        job = make_job(self.employer, tags=["python"])
        p = make_profile("p", tags=["python"])
        rematch_profile_obj(p)
        self.assertTrue(UserJobMatch.objects.filter(user=p.user, job=job).exists())

        # User excludes this employer -> job no longer qualifies -> row removed.
        p.excluded_employers = ["acme"]
        p.save()
        rematch_profile_obj(p)
        self.assertFalse(UserJobMatch.objects.filter(user=p.user, job=job).exists())

    def test_new_user_empty_window_completes_with_zero_rows(self):
        p = make_profile("p", tags=["python"])  # no jobs exist
        stats = rematch_profile_obj(p)
        self.assertEqual(stats["scored"], 0)
        self.assertEqual(UserJobMatch.objects.filter(user=p.user).count(), 0)

    def test_inactive_profile_clears_all_its_matches(self):
        job = make_job(self.employer, tags=["python"])
        p = make_profile("p", tags=["python"])
        match_job(job)
        self.assertEqual(UserJobMatch.objects.filter(user=p.user).count(), 1)

        p.is_active = False
        p.save()
        stats = rematch_profile_obj(p)
        self.assertTrue(stats["inactive"])
        self.assertEqual(UserJobMatch.objects.filter(user=p.user).count(), 0)

    def test_closed_jobs_excluded_from_rematch(self):
        closed = make_job(self.employer, "1", tags=["python"], status=Job.Status.CLOSED)
        p = make_profile("p", tags=["python"])
        rematch_profile_obj(p)
        self.assertFalse(UserJobMatch.objects.filter(user=p.user, job=closed).exists())
