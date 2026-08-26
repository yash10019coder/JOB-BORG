"""Add SmartRecruiters, Workable, Recruitee, Personio, and Oracle Cloud ATS choices."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0009_alter_discoveredboard_ats_alter_jobsource_ats"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discoveredboard",
            name="ats",
            field=models.CharField(
                choices=[
                    ("greenhouse", "Greenhouse"),
                    ("lever", "Lever"),
                    ("ashby", "Ashby"),
                    ("workday", "Workday"),
                    ("smartrecruiters", "SmartRecruiters"),
                    ("workable", "Workable"),
                    ("recruitee", "Recruitee"),
                    ("personio", "Personio"),
                    ("oracle_cloud", "Oracle Cloud"),
                ],
                default="greenhouse",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="jobsource",
            name="ats",
            field=models.CharField(
                choices=[
                    ("greenhouse", "Greenhouse"),
                    ("lever", "Lever"),
                    ("ashby", "Ashby"),
                    ("workday", "Workday"),
                    ("smartrecruiters", "SmartRecruiters"),
                    ("workable", "Workable"),
                    ("recruitee", "Recruitee"),
                    ("personio", "Personio"),
                    ("oracle_cloud", "Oracle Cloud"),
                ],
                default="greenhouse",
                max_length=32,
            ),
        ),
    ]
