"""Job + JobSource models — global/shared postings ingested from ATS boards."""
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from pgvector.django import VectorField


class JobSource(models.Model):
    """A DB-driven registry of ATS boards to ingest.

    Each source is a single company's board (Greenhouse boards are 1:1 with an
    employer), so a job's employer is resolved by the source's FK — never by
    fuzzy-matching a company name out of the API payload.
    """

    class ATS(models.TextChoices):
        GREENHOUSE = "greenhouse", "Greenhouse"

    ats = models.CharField(max_length=32, choices=ATS.choices, default=ATS.GREENHOUSE)
    board_token = models.CharField(
        max_length=255,
        help_text="ATS board identifier (e.g. Greenhouse board token).",
    )
    employer = models.ForeignKey(
        "employers.Employer",
        on_delete=models.CASCADE,
        related_name="job_sources",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether the hourly ingestion sweep pulls from this board.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ats", "board_token"],
                name="uniq_jobsource_ats_board_token",
            ),
        ]

    def __str__(self):
        return f"{self.get_ats_display()}:{self.board_token} ({self.employer})"


class Job(models.Model):
    """A single job posting, shared by all users.

    Match scoring lives in ``matching.UserJobMatch`` (per user), never here —
    a job's fit is a relationship to a profile, not a property of the job.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    # Idempotency key: a posting is uniquely identified by its ATS + external id.
    source_ats = models.CharField(max_length=32)
    source_job_id = models.CharField(max_length=255)

    employer = models.ForeignKey(
        "employers.Employer",
        on_delete=models.PROTECT,
        related_name="jobs",
    )

    title = models.CharField(max_length=512)
    description = models.TextField(blank=True, default="")

    # Location — free-text plus an explicit remote flag (never inferred from
    # the location string) so the matching pre-filter/scorer has a reliable key.
    location = models.CharField(max_length=255, blank=True, default="")
    is_remote = models.BooleanField(default=False)

    salary_min = models.IntegerField(null=True, blank=True)
    salary_max = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )

    # Classification (computed once per job by U8, not per user).
    classification_tags = models.JSONField(default=list, blank=True)
    ruleset_version = models.CharField(max_length=32, blank=True, default="")

    # Ingestion -> classification signalling (U6/U8).
    needs_classification = models.BooleanField(default=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")

    # Reserved for future semantic matching — added now to avoid a later
    # migration on this large table; left null in v1.
    embedding = VectorField(dimensions=384, null=True, blank=True)

    scraped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_ats", "source_job_id"],
                name="uniq_job_source_ats_source_job_id",
            ),
        ]
        indexes = [
            GinIndex(fields=["classification_tags"], name="job_tags_gin"),
            models.Index(fields=["status"], name="job_status_idx"),
            models.Index(fields=["is_remote"], name="job_is_remote_idx"),
            models.Index(fields=["location"], name="job_location_idx"),
            models.Index(
                fields=["needs_classification"], name="job_needs_class_idx"
            ),
        ]

    def __str__(self):
        return f"{self.title} @ {self.employer}"
