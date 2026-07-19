"""Backfill structured target-location fields on existing Profile rows.

See apps/jobs/migrations/0007_backfill_job_locations.py for the full
rationale (historical-model resolution, idempotency, race safety) --
this is the Profile-side counterpart.
"""
from django.db import migrations

from apps.locations.services import backfill_profiles


def forwards(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    backfill_profiles(Profile)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_profile_target_locations_alias_version_index"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
