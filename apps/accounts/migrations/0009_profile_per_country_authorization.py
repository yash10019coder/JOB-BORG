"""Replace the flat, US-centric `visa_status` / `citizenship` columns with
per-country fields.

`visa_status` answered "authorized to work here?" without saying where "here"
was, and its vocabulary (H-1B, OPT, CPT, O-1, TN, E-3) is entirely US/AU
specific -- so it silently supplied US answers to jobs in every country.
`visa_status_by_country` and `citizenship_countries` key that by ISO 3166-1
alpha-3 instead, which is what makes the answer country-specific.

The data step carries existing values forward rather than dropping them. The
old column had no country attached, but its vocabulary was US-centric, so
reading it as "the US" is the faithful interpretation -- anything else would
invent a country the user never stated. CPT folds into `opt`, since the new
vocabulary has a single "OPT or CPT (US)" status.
"""

from django.db import migrations, models

# Old flat `visa_status` value -> new per-country status value.
_CARRY_FORWARD = {
    "citizen": "citizen",
    "permanent_resident": "permanent_resident",
    "h1b": "h1b",
    "opt": "opt",
    "cpt": "opt",
    "o1": "o1",
    "tn": "tn",
    "e3": "e3",
    "other": "other",
    "not_authorized": "not_authorized",
}

# The old vocabulary was US-specific, so a value that carried no country of
# its own is read as the user's US status.
_ASSUMED_COUNTRY = "USA"


def carry_forward_authorization(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    for profile in Profile.objects.all().iterator():
        old_status = getattr(profile, "visa_status", "")
        if not old_status:
            continue
        status = _CARRY_FORWARD.get(old_status)
        if not status:
            continue
        profile.visa_status_by_country = {_ASSUMED_COUNTRY: status}
        profile.save(update_fields=["visa_status_by_country"])


def noop_reverse(apps, schema_editor):
    """Reverse is deliberately a no-op: collapsing per-country rows back into
    one US-centric column would mean picking a winner, and silently picking
    one recreates exactly the ambiguity this migration removes."""


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_profile_citizenship_profile_preferred_currency_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="profile",
            name="citizenship",
        ),
        migrations.RemoveField(
            model_name="profile",
            name="visa_status",
        ),
        migrations.AddField(
            model_name="profile",
            name="citizenship_countries",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="ISO alpha-3 codes of countries you are a citizen of.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="visa_status_by_country",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Per-country work authorization, e.g. {"IND": "citizen"}.',
            ),
        ),
        migrations.RunPython(carry_forward_authorization, noop_reverse),
    ]
