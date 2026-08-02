"""Manually re-run the location backfill outside a migration.

Useful for retrying after a partial failure, or for a fast-follow
re-normalization sweep before the periodic Celery sweep task picks it up.
Delegates to the same service functions the data migration uses -- the two
call sites differ only in which model class they pass in (live here,
historical inside the migration).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Profile
from apps.jobs.models import Job
from apps.locations.services import backfill_jobs, backfill_profiles, diff_stale_locations


class Command(BaseCommand):
    help = "Re-normalize Job and Profile locations not yet at the current alias-table version."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=None)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Report rows whose resolved value would change or regress "
                "under the current dataset version, without writing. Run "
                "before a version cutover to catch a same-type ambiguity "
                "tiebreak silently picking a different candidate than the "
                "previous dataset resolved uniquely."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="With --dry-run, emit the diff as machine-readable JSON instead of formatted text.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="With --dry-run, exit non-zero if any value-changing or regressing rows are found.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        if options["dry_run"]:
            self._dry_run(batch_size, as_json=options["json"], strict=options["strict"])
            return

        job_stats = backfill_jobs(Job, batch_size=batch_size)
        self.stdout.write(self.style.SUCCESS(f"Jobs: {job_stats['updated']} rows normalized"))

        profile_stats = backfill_profiles(Profile, batch_size=batch_size)
        self.stdout.write(
            self.style.SUCCESS(f"Profiles: {profile_stats['updated']} rows normalized")
        )

    def _dry_run(self, batch_size, *, as_json=False, strict=False):
        diff = diff_stale_locations(Job, Profile, batch_size=batch_size)
        job_changes = diff["job_changes"]
        profile_changes = diff["profile_changes"]
        has_changes = bool(job_changes or profile_changes)

        if as_json:
            self.stdout.write(json.dumps(diff))
        elif not has_changes:
            self.stdout.write(self.style.SUCCESS("Dry run: no value-changing resolutions."))
        else:
            for change in job_changes:
                self.stdout.write(
                    f"Job {change['pk']} ({change['location']!r}): "
                    f"{change['old']} -> {change['new']}"
                )
            for change in profile_changes:
                self.stdout.write(
                    f"Profile {change['pk']}: {change['old']} -> {change['new']}"
                )
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(job_changes)} job(s), {len(profile_changes)} profile(s) "
                    "would change value or regress -- review before cutting over."
                )
            )

        if strict and has_changes:
            raise CommandError(
                f"Dry run found {len(job_changes)} job(s), {len(profile_changes)} profile(s) "
                "that would change value or regress."
            )
