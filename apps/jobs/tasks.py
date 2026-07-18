"""Celery tasks for job ingestion.

- ``ingest_source`` fetches one board and upserts it.
- ``ingest_all_active_sources`` is the hourly Beat entry point; it isolates
  per-board failures so one bad board never aborts the others.
- ``discover_boards`` is the daily Beat entry point that grows the
  ``JobSource`` registry itself; see
  docs/plans/2026-07-18-004-feat-job-source-discovery-plan.md.
"""
import logging

from celery import shared_task

from .ingestion.board_search import BoardSearchClient
from .ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from .ingestion.greenhouse_client import GreenhouseClient
from .ingestion.upsert import upsert_jobs
from .models import DiscoveredBoard, JobSource

logger = logging.getLogger(__name__)


def enqueue_classification(job_ids):
    """Best-effort event-driven trigger for classification.

    Sent by name to avoid an import cycle with the classification app. The
    ``needs_classification`` flag set during upsert is the durable signal — the
    periodic classification sweep (U8) catches anything this enqueue misses.
    """
    if not job_ids:
        return
    from config.celery import app

    app.send_task("apps.classification.classify_jobs", kwargs={"job_ids": list(job_ids)})


@shared_task(name="apps.jobs.ingest_source")
def ingest_source(source_id):
    """Fetch + upsert a single JobSource. Returns a stats dict."""
    source = JobSource.objects.select_related("employer").get(pk=source_id)
    client = GreenhouseClient()
    normalized = client.fetch_jobs(source.board_token)
    result = upsert_jobs(source, normalized)
    enqueue_classification(result.needs_classification_ids)
    stats = {
        "source_id": source_id,
        "created": len(result.created_ids),
        "updated": len(result.updated_ids),
        "reopened": len(result.reopened_ids),
        "unchanged": len(result.unchanged_ids),
        "closed": len(result.closed_ids),
    }
    logger.info("Ingested source %s: %s", source_id, stats)
    return stats


@shared_task(name="apps.jobs.ingest_all_active_sources")
def ingest_all_active_sources():
    """Hourly Beat entry point — ingest every active board independently."""
    results = []
    for source_id in JobSource.objects.filter(is_active=True).values_list(
        "id", flat=True
    ):
        try:
            results.append(ingest_source(source_id))
        except Exception:  # noqa: BLE001 — one board's failure must not abort others
            logger.exception("Ingestion failed for source %s", source_id)
            results.append({"source_id": source_id, "error": True})
    return results


@shared_task(name="apps.jobs.discover_boards")
def discover_boards():
    """Daily Beat entry point — search for new Greenhouse boards and queue
    validated candidates as pending ``DiscoveredBoard`` rows for reviewer
    approval. Never raises: a failed search step or a failed validation call
    is logged and reflected in the run's stats instead of aborting the task,
    matching ``ingest_all_active_sources``'s per-item isolation posture.
    """
    search_result = BoardSearchClient().search_greenhouse_boards()

    # A token can only ever have one JobSource or one DiscoveredBoard row
    # (both enforce a UniqueConstraint on (ats, board_token)) — excluding
    # every known token, regardless of JobSource.is_active or DiscoveredBoard
    # .status, is what keeps re-validation and duplicate-create errors out of
    # this loop, not just the "active"/"pending" cases R3 names informally.
    known_tokens = set(
        JobSource.objects.filter(ats=JobSource.ATS.GREENHOUSE).values_list(
            "board_token", flat=True
        )
    ) | set(
        DiscoveredBoard.objects.filter(ats=JobSource.ATS.GREENHOUSE).values_list(
            "board_token", flat=True
        )
    )

    stats = {
        "found": len(search_result.tokens),
        "already_known": 0,
        "validated": 0,
        "failed": 0,
        "search_failed": search_result.failed,
    }

    client = GreenhouseClient()
    for token in search_result.tokens:
        if token in known_tokens:
            stats["already_known"] += 1
            continue

        try:
            jobs = client.fetch_jobs(token)
        except (GreenhouseUnavailable, GreenhouseParseError):
            logger.exception("Discovery validation failed for token %s", token)
            stats["failed"] += 1
            continue

        DiscoveredBoard.objects.create(
            ats=JobSource.ATS.GREENHOUSE,
            board_token=token,
            derived_employer_name=token.replace("-", " ").title(),
            discovered_job_count=len(jobs),
        )
        stats["validated"] += 1

    logger.info("Discovery run: %s", stats)
    return stats
