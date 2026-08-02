"""Per-user Profile — matching criteria + who-you-are fields.

The built-in Django User is the auth/users table (see Key Decisions); Profile
is a OneToOne extension holding everything the matching fan-out reads.
"""
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def resume_upload_path(instance, filename):
    """Per-user resume path -- keeps uploads from colliding across users
    regardless of which storage backend (local/S3) is active."""
    return f"resumes/{instance.user_id}/{filename}"


def validate_resume_file(file):
    """Enforce the resume upload allowlist (U1) before the file is ever
    handed to the parsing task -- a storage/DoS control and a first line of
    defense against malicious uploads, independent of the parsing task's own
    bounded time limit (see apps/accounts/resume_parsing.py).
    """
    max_size = getattr(settings, "RESUME_MAX_UPLOAD_SIZE_BYTES", 10 * 1024 * 1024)
    if file.size > max_size:
        raise ValidationError(
            f"Resume file is too large ({file.size} bytes); max is {max_size} bytes."
        )

    allowed_extensions = getattr(
        settings, "RESUME_ALLOWED_EXTENSIONS", [".pdf", ".docx", ".txt"]
    )
    ext = os.path.splitext(file.name or "")[1].lower()
    if ext not in allowed_extensions:
        raise ValidationError(
            f"Unsupported resume file type '{ext}'. "
            f"Allowed types: {', '.join(allowed_extensions)}."
        )

    # Only in-flight uploads (UploadedFile) carry a browser-reported
    # content_type; a FieldFile re-validated from storage does not, so this
    # check is best-effort on top of the extension check above, not a
    # replacement for it. A FieldFile doesn't proxy arbitrary attributes to
    # its wrapped file, so check both `file.content_type` (raw UploadedFile)
    # and `file.file.content_type` (FieldFile wrapping an uncommitted
    # UploadedFile, as with a freshly-assigned `profile.resume`).
    content_type = getattr(file, "content_type", None)
    if content_type is None:
        content_type = getattr(getattr(file, "file", None), "content_type", None)
    allowed_content_types = getattr(
        settings,
        "RESUME_ALLOWED_CONTENT_TYPES",
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        ],
    )
    if content_type is not None and content_type not in allowed_content_types:
        raise ValidationError(f"Unsupported resume content type '{content_type}'.")


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
    phone = models.CharField(max_length=32, blank=True, default="")
    linkedin_url = models.URLField(max_length=255, blank=True, default="")

    # Resume -- standard-field source for auto-apply drafting (see
    # docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md U1).
    # `resume_text` is populated asynchronously by the `parse_resume` Celery
    # task (apps/accounts/resume_parsing.py), explicitly enqueued from
    # `Profile.set_resume()` -- deliberately not a post_save signal, so the
    # trigger is visible at the call site rather than implicit. It stays
    # empty until parsing completes, or forever if no resume is uploaded or
    # nothing was extractable; downstream consumers must treat empty as
    # "no resume text available", not an error.
    resume = models.FileField(
        upload_to=resume_upload_path,
        blank=True,
        null=True,
        validators=[validate_resume_file],
        help_text="PDF/DOCX/TXT only. Parsed into resume_text asynchronously.",
    )
    resume_text = models.TextField(blank=True, default="")

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
        max_length=32, blank=True, default="", db_index=True
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

    def set_resume(self, file):
        """Assign an uploaded resume file, or clear it if `file` is falsy,
        and explicitly enqueue async text extraction (skipped on clear).

        This is the one call site every write path to `resume` should go
        through -- add *and* clear -- (the profile-edit view once it exists
        in apps/web, and `ProfileAdmin.save_model` today) so parsing is
        triggered the same way everywhere and there's no second, divergent
        path for clearing. Deliberately not a `post_save` signal, per U1's
        approach, so the trigger stays visible here instead of implicit.
        """
        self.resume = file or None
        self.resume_text = ""
        self.full_clean(validate_unique=False)
        self.save(update_fields=["resume", "resume_text", "updated_at"])

        if file:
            from .resume_parsing import parse_resume

            parse_resume.delay(self.pk)

    def __str__(self):
        return f"Profile<{self.user.username}>"
