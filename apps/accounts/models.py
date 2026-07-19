"""Per-user Profile — matching criteria + who-you-are fields.

The built-in Django User is the auth/users table (see Key Decisions); Profile
is a OneToOne extension holding everything the matching fan-out reads.
"""
from django.conf import settings
from django.db import models


class Profile(models.Model):
    class RemotePref(models.TextChoices):
        ANY = "any", "Any"
        REMOTE_ONLY = "remote_only", "Remote only"
        ONSITE_ONLY = "onsite_only", "On-site only"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    # Who-you-are.
    full_name = models.CharField(max_length=255, blank=True, default="")
    headline = models.CharField(max_length=255, blank=True, default="")

    # Matching criteria.
    target_titles = models.JSONField(default=list, blank=True)
    # Skill/keyword list scored against a job's classification_tags to produce
    # matched_tags — the profile-side counterpart the scorer intersects with.
    target_tags = models.JSONField(default=list, blank=True)
    target_locations = models.JSONField(default=list, blank=True)
    # Structured mirror of target_locations, one entry per raw string, each
    # shaped {"raw": str, "city": str|None, "region": str|None,
    # "country": str|None, "resolved": bool} — computed by ProfileForm via
    # apps.locations.engine.normalize_location whenever target_locations
    # changes. target_locations itself stays untouched (raw, user-typed) so
    # the CSV form field round-trips exactly what the user entered.
    target_locations_normalized = models.JSONField(default=list, blank=True)
    target_locations_alias_version = models.CharField(
        max_length=32, blank=True, default=""
    )
    excluded_employers = models.JSONField(
        default=list,
        blank=True,
        help_text="Employer slugs to exclude from recommendations.",
    )
    min_salary = models.IntegerField(null=True, blank=True)
    remote_pref = models.CharField(
        max_length=16,
        choices=RemotePref.choices,
        default=RemotePref.ANY,
    )

    # Gates whether this profile participates in matching fan-out at all.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile<{self.user.username}>"
