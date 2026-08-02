"""Command-layer coverage for `backfill_locations --dry-run`.

apps/locations/tests/test_services.py already covers diff_stale_locations()
thoroughly; this file covers the thin CLI wrapper around it -- arg wiring,
the --json machine-readable output, and the --strict exit-code contract --
none of which the service-level tests exercise.
"""
import json
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.accounts.models import Profile
from apps.employers.models import Employer
from apps.jobs.models import Job, JobSource


def _make_job(source_job_id, location="Austin, TX, US"):
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
        needs_classification=False,
    )


class DryRunCommandTests(TestCase):
    def test_dry_run_with_no_changes_writes_success_and_no_rows(self):
        job = _make_job(1)
        job.location_city = "Austin"
        job.location_region = "TX"
        job.location_country = "US"
        job.location_resolved = True
        job.save(update_fields=["location_city", "location_region", "location_country", "location_resolved"])

        out = StringIO()
        call_command("backfill_locations", "--dry-run", stdout=out)

        self.assertIn("no value-changing resolutions", out.getvalue())
        job.refresh_from_db()
        self.assertEqual(job.location_alias_version, "")

    def test_dry_run_with_changes_reports_row_details(self):
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
            out = StringIO()
            call_command("backfill_locations", "--dry-run", stdout=out)
        finally:
            services_module.normalize_location = original

        output = out.getvalue()
        self.assertIn(str(job.pk), output)
        self.assertIn("1 job(s)", output)

    def test_dry_run_json_emits_machine_readable_diff(self):
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
            out = StringIO()
            call_command("backfill_locations", "--dry-run", "--json", stdout=out)
        finally:
            services_module.normalize_location = original

        parsed = json.loads(out.getvalue())
        self.assertEqual(len(parsed["job_changes"]), 1)
        self.assertEqual(parsed["job_changes"][0]["pk"], job.pk)

    def test_dry_run_strict_raises_when_changes_found(self):
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
            with self.assertRaises(CommandError):
                call_command("backfill_locations", "--dry-run", "--strict", stdout=StringIO())
        finally:
            services_module.normalize_location = original

    def test_dry_run_strict_is_a_no_op_when_no_changes_found(self):
        job = _make_job(1)
        job.location_city = "Austin"
        job.location_region = "TX"
        job.location_country = "US"
        job.location_resolved = True
        job.save(update_fields=["location_city", "location_region", "location_country", "location_resolved"])

        out = StringIO()
        call_command("backfill_locations", "--dry-run", "--strict", stdout=out)
        self.assertIn("no value-changing resolutions", out.getvalue())
