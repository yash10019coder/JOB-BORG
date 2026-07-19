"""Index target_locations_alias_version before the backfill runs.

Same rationale as apps/jobs/migrations/0006_job_location_alias_version_index.py.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_profile_target_locations_alias_version_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="target_locations_alias_version",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
    ]
