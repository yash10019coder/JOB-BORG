"""Idempotent upsert of normalized jobs into the shared ``jobs`` table.

Keyed on the ``(source_ats, source_job_id)`` unique constraint. A job's
employer is resolved from the JobSource FK, never from the payload. Closure
detection marks any previously-open job for the source that is absent from the
current fetch as ``closed``.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.jobs.models import Job

from .config import compute_content_hash

# Fields copied from a normalized dict onto the Job row. Structured location
# fields are derived purely from `location` (already hashed below via
# apps/jobs/ingestion/config.py's _HASH_FIELDS) plus a deploy-time-constant
# alias-table version, so they belong here but deliberately do NOT join
# _HASH_FIELDS -- see that module's docstring.
_CONTENT_FIELDS = (
    "title",
    "description",
    "location",
    "is_remote",
    "location_city",
    "location_region",
    "location_country",
    "location_resolved",
    "location_alias_version",
    "salary_min",
    "salary_max",
    "source_url",
)


@dataclass
class UpsertResult:
    created_ids: list = field(default_factory=list)
    updated_ids: list = field(default_factory=list)
    reopened_ids: list = field(default_factory=list)
    unchanged_ids: list = field(default_factory=list)
    closed_ids: list = field(default_factory=list)

    @property
    def needs_classification_ids(self):
        """Job ids that entered/re-entered the pipeline and must be classified."""
        return self.created_ids + self.updated_ids + self.reopened_ids


@transaction.atomic
def upsert_jobs(job_source, normalized_jobs, *, now=None):
    """Upsert a board's jobs and detect closures. Atomic: all-or-nothing."""
    now = now or timezone.now()
    result = UpsertResult()
    seen_source_job_ids = set()

    for nj in normalized_jobs:
        source_ats = nj["source_ats"]
        source_job_id = nj["source_job_id"]
        seen_source_job_ids.add(source_job_id)
        content_hash = compute_content_hash(nj)

        job = Job.objects.filter(
            source_ats=source_ats, source_job_id=source_job_id
        ).first()

        if job is None:
            job = Job(
                source_ats=source_ats,
                source_job_id=source_job_id,
                employer=job_source.employer,
                status=Job.Status.OPEN,
                content_hash=content_hash,
                needs_classification=True,
                scraped_at=now,
            )
            _apply_content(job, nj)
            job.save()
            result.created_ids.append(job.id)
            continue

        was_closed = job.status == Job.Status.CLOSED
        content_changed = job.content_hash != content_hash

        job.scraped_at = now

        if was_closed:
            # Reopen: funnel the posting back through the full pipeline so its
            # matches (deleted on closure) are recomputed.
            job.status = Job.Status.OPEN
            job.classification_tags = []
            job.ruleset_version = ""
            job.needs_classification = True
            job.content_hash = content_hash
            _apply_content(job, nj)
            job.save()
            result.reopened_ids.append(job.id)
        elif content_changed:
            job.content_hash = content_hash
            job.needs_classification = True
            _apply_content(job, nj)
            job.save()
            result.updated_ids.append(job.id)
        else:
            job.save(update_fields=["scraped_at", "updated_at"])
            result.unchanged_ids.append(job.id)

    # Closure detection — open jobs for this source absent from the fetch.
    stale = Job.objects.filter(
        source_ats=job_source.ats,
        employer=job_source.employer,
        status=Job.Status.OPEN,
    ).exclude(source_job_id__in=seen_source_job_ids)
    result.closed_ids = list(stale.values_list("id", flat=True))
    if result.closed_ids:
        stale.update(status=Job.Status.CLOSED, updated_at=now)

    return result


def _apply_content(job, normalized_job):
    for f in _CONTENT_FIELDS:
        setattr(job, f, normalized_job[f])
