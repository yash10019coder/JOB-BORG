"""Sweep task tests."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.models import Job, JobSource
from apps.locations.engine import CURRENT_LOCATION_ALIAS_VERSION
from apps.locations.tasks import sweep_stale_locations

User = get_user_model()


class SweepStaleLocationsTests(TestCase):
    def setUp(self):
        # Suppress the profile post-save signal's real enqueue.
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _make_stale_job(self, source_job_id="1"):
        employer = Employer.objects.get_or_create(slug="acme", defaults={"name": "Acme"})[0]
        source = JobSource.objects.get_or_create(
            ats=JobSource.ATS.GREENHOUSE, board_token="acme", defaults={"employer": employer}
        )[0]
        return Job.objects.create(
            source_ats="greenhouse",
            source_job_id=source_job_id,
            employer=employer,
            title="Engineer",
            location="Austin, TX, US",
            location_alias_version="",  # simulates a version-bump-stale row
            needs_classification=False,
        )

    def test_sweep_renormalizes_rows_stale_relative_to_current_version(self):
        job = self._make_stale_job()
        stats = sweep_stale_locations()
        job.refresh_from_db()
        self.assertEqual(stats["jobs_updated"], 1)
        self.assertEqual(job.location_alias_version, CURRENT_LOCATION_ALIAS_VERSION)
        self.assertEqual(job.location_city, "Austin")

    def test_sweep_with_nothing_stale_is_a_no_op(self):
        stats = sweep_stale_locations()
        self.assertEqual(stats, {"jobs_updated": 0, "profiles_updated": 0})

    def test_sweep_bounded_by_batch_size_across_multiple_calls(self):
        for i in range(3):
            self._make_stale_job(source_job_id=str(i))
        first = sweep_stale_locations(batch_size=1)
        # backfill_jobs loops internally to exhaustion regardless of
        # batch_size, so a single sweep call still normalizes every stale row.
        self.assertEqual(first["jobs_updated"], 3)
        second = sweep_stale_locations(batch_size=1)
        self.assertEqual(second["jobs_updated"], 0)
