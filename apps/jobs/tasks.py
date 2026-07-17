"""Celery tasks for job ingestion.

- ``ingest_source`` fetches one board and upserts it.
- ``ingest_all_active_sources`` is the hourly Beat entry point; it isolates
  per-board failures so one bad board never aborts the others.
"""
import logging

from celery import shared_task

from .ingestion.greenhouse_client import GreenhouseClient
from .ingestion.upsert import upsert_jobs
from .models import JobSource

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
