"""Manually re-run the location backfill outside a migration.

Useful for retrying after a partial failure, or for a fast-follow
re-normalization sweep before the periodic Celery sweep task picks it up.
Delegates to the same service functions the data migration uses -- the two
call sites differ only in which model class they pass in (live here,
historical inside the migration).
"""
from django.core.management.base import BaseCommand

from apps.accounts.models import Profile
from apps.jobs.models import Job
from apps.locations.services import backfill_jobs, backfill_profiles


class Command(BaseCommand):
    help = "Re-normalize Job and Profile locations not yet at the current alias-table version."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=None)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        job_stats = backfill_jobs(Job, batch_size=batch_size)
        self.stdout.write(self.style.SUCCESS(f"Jobs: {job_stats['updated']} rows normalized"))

        profile_stats = backfill_profiles(Profile, batch_size=batch_size)
        self.stdout.write(
            self.style.SUCCESS(f"Profiles: {profile_stats['updated']} rows normalized")
        )
