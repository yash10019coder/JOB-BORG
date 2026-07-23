"""Backfill service tests.

Uses the real Job/Profile models (DB-backed, TestCase) since the service
functions operate on querysets -- but they accept the model class as a
parameter rather than importing it, so the same functions are exercised here
exactly as a migration's historical model or the management command's live
model would call them.
"""
from django.test import TestCase

from apps.accounts.models import Profile
from apps.employers.models import Employer
from apps.jobs.models import Job, JobSource
from apps.locations.engine import CURRENT_LOCATION_ALIAS_VERSION
from apps.locations.services import (
    backfill_jobs,
    backfill_profiles,
    diff_stale_locations,
    normalize_target_locations,
)


def _make_job(source_job_id, location="New York, NY, US", alias_version=""):
    employer = Employer.objects.get_or_create(slug="acme", defaults={"name": "Acme"})[0]
    source = JobSource.objects.get_or_create(
        ats=JobSource.ATS.GREENHOUSE, board_token="acme", defaults={"employer": employer}
    )[0]
    return Job.objects.create(
        source_ats="greenhouse",
        source_job_id=str(source_job_id),
        employer=employer,
        title="Engineer",
        location=location,
        location_alias_version=alias_version,
        needs_classification=False,
    )


class BackfillJobsTests(TestCase):
    def test_unnormalized_rows_get_structured_fields(self):
        job = _make_job(1, location="Austin, TX, US")
        backfill_jobs(Job)
        job.refresh_from_db()
        self.assertEqual(job.location_city, "Austin")
        self.assertEqual(job.location_region, "TX")
        self.assertEqual(job.location_country, "US")
        self.assertTrue(job.location_resolved)
        self.assertEqual(job.location_alias_version, CURRENT_LOCATION_ALIAS_VERSION)

    def test_already_current_row_is_skipped_not_reprocessed(self):
        job = _make_job(1, location="Austin, TX, US",
                         alias_version=CURRENT_LOCATION_ALIAS_VERSION)
        job.location_city = "Manually Set"
        job.save(update_fields=["location_city"])

        backfill_jobs(Job)

        job.refresh_from_db()
        self.assertEqual(job.location_city, "Manually Set")

    def test_rerun_is_a_safe_no_op(self):
        _make_job(1, location="Austin, TX, US")
        first = backfill_jobs(Job)
        second = backfill_jobs(Job)
        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)

    def test_batch_size_smaller_than_row_count_processes_all_rows(self):
        for i in range(5):
            _make_job(i, location="Austin, TX, US")
        result = backfill_jobs(Job, batch_size=2)
        self.assertEqual(result["updated"], 5)
        self.assertEqual(
            Job.objects.filter(location_alias_version=CURRENT_LOCATION_ALIAS_VERSION).count(),
            5,
        )

    def test_concurrently_advanced_row_is_not_regressed(self):
        # Simulates U3's ingestion path writing fresh, current data to a row
        # between the backfill's read and write phases -- the backfill must
        # not overwrite it with stale data computed from the old location.
        job = _make_job(1, location="Austin, TX, US")

        real_normalize = __import__(
            "apps.locations.services", fromlist=["normalize_location"]
        ).normalize_location

        def racing_normalize(raw):
            # Simulate the race: a concurrent writer advances the row to the
            # current version with different (fresh) data right after this
            # backfill call reads the row but before it writes.
            Job.objects.filter(pk=job.pk).update(
                location_city="Concurrent City",
                location_region="CC",
                location_country="US",
                location_resolved=True,
                location_alias_version=CURRENT_LOCATION_ALIAS_VERSION,
            )
            return real_normalize(raw)

        import apps.locations.services as services_module
        original = services_module.normalize_location
        services_module.normalize_location = racing_normalize
        try:
            backfill_jobs(Job)
        finally:
            services_module.normalize_location = original

        job.refresh_from_db()
        self.assertEqual(job.location_city, "Concurrent City")
        self.assertEqual(job.location_region, "CC")

    def test_no_signals_or_side_effect_tasks_triggered(self):
        job = _make_job(1, location="Austin, TX, US")
        job.needs_classification = False
        job.save(update_fields=["needs_classification"])

        backfill_jobs(Job)

        job.refresh_from_db()
        self.assertFalse(job.needs_classification)


class BackfillProfilesTests(TestCase):
    def _make_profile(self, username, locations, alias_version=""):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(username=username, password="pw")
        profile = user.profile
        profile.target_locations = locations
        profile.target_locations_alias_version = alias_version
        profile.save()
        return profile

    def test_unnormalized_profile_gets_structured_mirror(self):
        profile = self._make_profile("alice", ["New York", "London"])
        backfill_profiles(Profile)
        profile.refresh_from_db()
        self.assertEqual(len(profile.target_locations_normalized), 2)
        self.assertEqual(profile.target_locations_alias_version, CURRENT_LOCATION_ALIAS_VERSION)

    def test_already_current_profile_is_skipped(self):
        profile = self._make_profile(
            "bob", ["New York"], alias_version=CURRENT_LOCATION_ALIAS_VERSION
        )
        profile.target_locations_normalized = [{"raw": "sentinel"}]
        profile.save(update_fields=["target_locations_normalized"])

        backfill_profiles(Profile)

        profile.refresh_from_db()
        self.assertEqual(profile.target_locations_normalized, [{"raw": "sentinel"}])

    def test_no_rematch_side_effect(self):
        from unittest import mock

        with mock.patch("apps.matching.signals.schedule_rematch") as mocked:
            self._make_profile("carol", ["New York"])
            mocked.reset_mock()
            backfill_profiles(Profile)
            mocked.assert_not_called()


class DiffStaleLocationsTests(TestCase):
    """Covers U4's pre-cutover safety check: a read-only preview of what
    backfill would change, restricted to rows whose *value* would change or
    regress -- not rows that would merely newly-resolve."""

    def _make_profile(self, username, locations, normalized, alias_version=""):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(username=username, password="pw")
        profile = user.profile
        profile.target_locations = locations
        profile.target_locations_normalized = normalized
        profile.target_locations_alias_version = alias_version
        profile.save()
        return profile

    def test_value_changing_resolution_is_reported(self):
        job = _make_job(1, location="Springfield")
        job.location_city = "Springfield"
        job.location_region = "IL"
        job.location_country = "US"
        job.location_resolved = True
        job.save(update_fields=["location_city", "location_region", "location_country", "location_resolved"])

        import apps.locations.services as services_module
        original = services_module.normalize_location
        services_module.normalize_location = lambda raw: {
            "city": "Springfield", "region": "MA", "country": "US", "resolved": True,
        }
        try:
            diff = diff_stale_locations(Job, Profile)
        finally:
            services_module.normalize_location = original

        self.assertEqual(len(diff["job_changes"]), 1)
        self.assertEqual(diff["job_changes"][0]["pk"], job.pk)
        self.assertEqual(diff["job_changes"][0]["old"]["region"], "IL")
        self.assertEqual(diff["job_changes"][0]["new"]["region"], "MA")

    def test_unchanged_resolution_is_not_reported(self):
        job = _make_job(1, location="Austin, TX, US")
        job.location_city = "Austin"
        job.location_region = "TX"
        job.location_country = "US"
        job.location_resolved = True
        job.save(update_fields=["location_city", "location_region", "location_country", "location_resolved"])

        diff = diff_stale_locations(Job, Profile)
        self.assertEqual(diff["job_changes"], [])

    def test_newly_resolving_row_is_not_reported(self):
        # Previously-unresolved rows becoming resolved is the desired,
        # expected outcome of a dataset swap -- reporting every one of these
        # would drown the actual signal (a value CHANGING) in noise.
        _make_job(1, location="São Paulo")
        diff = diff_stale_locations(Job, Profile)
        self.assertEqual(diff["job_changes"], [])

    def test_dry_run_writes_nothing(self):
        job = _make_job(1, location="Austin, TX, US")
        diff_stale_locations(Job, Profile)
        job.refresh_from_db()
        self.assertEqual(job.location_alias_version, "")
        self.assertFalse(job.location_resolved)

    def test_profile_value_change_is_reported(self):
        profile = self._make_profile(
            "gina",
            ["Springfield"],
            [{"raw": "Springfield", "city": "Springfield", "region": "IL", "country": "US", "resolved": True}],
        )

        import apps.locations.services as services_module
        original = services_module.normalize_location
        services_module.normalize_location = lambda raw: {
            "city": "Springfield", "region": "MA", "country": "US", "resolved": True,
        }
        try:
            diff = diff_stale_locations(Job, Profile)
        finally:
            services_module.normalize_location = original

        self.assertEqual(len(diff["profile_changes"]), 1)
        self.assertEqual(diff["profile_changes"][0]["pk"], profile.pk)

    def test_profile_with_an_additional_newly_resolving_entry_is_not_reported(self):
        # Code-review regression: a profile whose first entry ("Chicago")
        # was already resolved and stays the same, but whose SECOND raw
        # entry ("Xyzzyville") newly resolves under v2, must not be flagged
        # -- new_keys gaining an entry beyond old_keys is exactly the
        # desired outcome of the dataset swap, not a value change to review.
        self._make_profile(
            "henry",
            ["Chicago", "Xyzzyville"],
            [{"raw": "Chicago", "city": "Chicago", "region": "IL", "country": "US", "resolved": True}],
        )

        import apps.locations.services as services_module
        original = services_module.normalize_location

        def fake_normalize(raw):
            if raw == "Chicago":
                return {"city": "Chicago", "region": "IL", "country": "US", "resolved": True}
            return {"city": "Xyzzyville", "region": None, "country": "US", "resolved": True}

        services_module.normalize_location = fake_normalize
        try:
            diff = diff_stale_locations(Job, Profile)
        finally:
            services_module.normalize_location = original

        self.assertEqual(diff["profile_changes"], [])


class NormalizeTargetLocationsTests(TestCase):
    def test_dedupes_on_structured_tuple(self):
        result = normalize_target_locations(["SF", "San Francisco"])
        self.assertEqual(len(result), 1)

    def test_empty_list(self):
        self.assertEqual(normalize_target_locations([]), [])
