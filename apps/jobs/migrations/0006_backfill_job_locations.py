"""Backfill structured location fields on existing Job rows.

The repo's first RunPython data migration -- delegates to
apps.locations.services.backfill_jobs so the same, independently-tested
logic runs here and from the manual management command. Resolves Job via
the migration's historical model registry (not a live import), per
Django's documented data-migration convention: a live import can silently
diverge from the schema state a migration replay expects if a later
migration renames or removes a field this backfill relies on.

Idempotent by construction (see apps.locations.services.backfill_jobs) --
safe to interleave with concurrent ingestion writes and safe to re-run.
"""
from django.db import migrations

from apps.locations.services import backfill_jobs


def forwards(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    backfill_jobs(Job)


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0005_job_location_alias_version_job_location_city_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
