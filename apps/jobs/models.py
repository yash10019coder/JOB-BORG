"""Job + JobSource models — global/shared postings ingested from ATS boards."""
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from pgvector.django import VectorField

# Single source of truth for the full-text search config. Any query filtering
# against ``job_search_gin`` (see Job.Meta.indexes) must use this exact value
# — a mismatch doesn't error, it silently falls back to a sequential scan.
JOB_SEARCH_CONFIG = "english"


class JobSource(models.Model):
    """A DB-driven registry of ATS boards to ingest.

    Each source is a single company's board (Greenhouse boards are 1:1 with an
    employer), so a job's employer is resolved by the source's FK — never by
    fuzzy-matching a company name out of the API payload.
    """

    class ATS(models.TextChoices):
        GREENHOUSE = "greenhouse", "Greenhouse"
        LEVER = "lever", "Lever"
        ASHBY = "ashby", "Ashby"

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


class DiscoveredBoard(models.Model):
    """A candidate Greenhouse board found by the discovery pipeline.

    Kept as a separate model from ``JobSource`` rather than a status flag on
    it — the hourly ingestion sweep filters ``JobSource.is_active`` on every
    run, and review-queue-only fields (status, discovered_job_count, ...)
    would sit unused on every row that query touches.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    ats = models.CharField(
        max_length=32, choices=JobSource.ATS.choices, default=JobSource.ATS.GREENHOUSE
    )
    board_token = models.CharField(
        max_length=255,
        help_text="ATS board identifier extracted from the discovery search result.",
    )
    source_url = models.URLField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Where this candidate was found (the search result URL).",
    )
    derived_employer_name = models.CharField(max_length=255)
    discovered_job_count = models.IntegerField(
        default=0,
        help_text=(
            "Open job count seen at discovery-time validation — compared "
            "against the approval-time re-fetch to flag a board that "
            "changed materially between discovery and review."
        ),
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )

    discovered_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ats", "board_token"],
                name="uniq_discoveredboard_ats_board_token",
            ),
        ]

    def __str__(self):
        return f"{self.get_ats_display()}:{self.board_token} ({self.get_status_display()})"


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
    # Link back to the original posting (shown on the recommendation card).
    source_url = models.URLField(max_length=1024, blank=True, default="")

    # Location — free-text plus an explicit remote flag (never inferred from
    # the location string) so the matching pre-filter/scorer has a reliable key.
    location = models.CharField(max_length=255, blank=True, default="")
    is_remote = models.BooleanField(default=False)

    # Structured location, derived once at ingestion from ``location`` via
    # apps.locations.engine.normalize_location (never re-derived downstream —
    # same posture as is_remote above). location_resolved=False means the
    # curated alias table couldn't resolve this string; scoring falls back to
    # substring matching against the raw ``location`` field in that case.
    location_city = models.CharField(max_length=255, blank=True, default="")
    location_region = models.CharField(max_length=255, blank=True, default="")
    location_country = models.CharField(max_length=255, blank=True, default="")
    location_resolved = models.BooleanField(default=False)
    location_alias_version = models.CharField(max_length=32, blank=True, default="", db_index=True)

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
            models.Index(fields=["location_resolved"], name="job_location_resolved_idx"),
            # Full-text search over title/description (search bar). config
            # must be a literal string, not the default get_current_ts_config()
            # lookup — Postgres rejects non-IMMUTABLE functions in an index
            # expression. Queries against this index must use the same
            # config="english" or they'll silently fall back to a seq scan.
            GinIndex(
                SearchVector("title", "description", config=JOB_SEARCH_CONFIG),
                name="job_search_gin",
            ),
        ]

    def __str__(self):
        return f"{self.title} @ {self.employer}"
