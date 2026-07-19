"""Index location_alias_version before the backfill runs.

Without this, the backfill's `.exclude(location_alias_version=...)` query
(apps/locations/services.py backfill_jobs) forces a sequential scan every
batch iteration, growing more expensive as already-migrated rows accumulate
ahead of the still-stale ones in pk order.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0005_job_location_alias_version_job_location_city_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="location_alias_version",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
    ]
