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
from django.conf import settings
from django.db import transaction

from .ingestion.board_search import BoardSearchClient
from .ingestion.dispatch import get_client
from .ingestion.exceptions import IngestionParseError, IngestionUnavailable
from .ingestion.register import derive_employer_name
from .ingestion.upsert import upsert_jobs
from .models import DiscoveredBoard, JobSource

logger = logging.getLogger(__name__)

# Platforms discover_boards searches, in order.
_DISCOVERY_ATS_PLATFORMS = (
    JobSource.ATS.GREENHOUSE,
    JobSource.ATS.LEVER,
    JobSource.ATS.ASHBY,
    JobSource.ATS.WORKDAY,
)


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
    client = get_client(source.ats)
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


@shared_task(name="apps.jobs.discover_boards", time_limit=600, soft_time_limit=540)
def discover_boards():
    """Daily Beat entry point — search every platform in
    ``_DISCOVERY_ATS_PLATFORMS`` for new boards and queue validated
    candidates as pending ``DiscoveredBoard`` rows for reviewer approval.
    Never raises: a failed search step or a failed validation call is logged
    and reflected in the run's stats instead of aborting the task, matching
    ``ingest_all_active_sources``'s per-item isolation posture -- and one
    platform's failure doesn't stop the others from being searched.
    """
    stats = {
        "found": 0,
        "already_known": 0,
        "validated": 0,
        "failed": 0,
        "search_failed": False,
        "skipped_for_cap": 0,
    }
    for ats in _DISCOVERY_ATS_PLATFORMS:
        platform_stats = _discover_boards_for_ats(ats)
        for key in ("found", "already_known", "validated", "failed", "skipped_for_cap"):
            stats[key] += platform_stats[key]
        stats["search_failed"] = stats["search_failed"] or platform_stats["search_failed"]

    logger.info("Discovery run: %s", stats)
    return stats


def _discover_boards_for_ats(ats):
    search_result = BoardSearchClient().search_boards(ats)

    # A token can only ever have one JobSource or one DiscoveredBoard row for
    # a given ats (both enforce a UniqueConstraint on (ats, board_token)) --
    # excluding every known token, regardless of JobSource.is_active or
    # DiscoveredBoard.status, is what keeps re-validation and duplicate-create
    # errors out of this loop, not just the "active"/"pending" cases R3 names
    # informally.
    known_tokens = set(
        JobSource.objects.filter(ats=ats).values_list("board_token", flat=True)
    ) | set(
        DiscoveredBoard.objects.filter(ats=ats).values_list("board_token", flat=True)
    )

    # The candidate dataset can hand back thousands of tokens at once
    # (unlike a paginated search engine query); capping how many new ones get
    # validated and queued per run (per platform) keeps reviewer throughput
    # -- not candidate-source volume -- the bottleneck on growth, per R7/
    # success criteria.
    new_tokens = [token for token in search_result.tokens if token not in known_tokens]
    capped_tokens = new_tokens[: settings.DISCOVERY_MAX_NEW_BOARDS_PER_RUN]

    stats = {
        "found": len(search_result.tokens),
        "already_known": len(search_result.tokens) - len(new_tokens),
        "validated": 0,
        "failed": 0,
        "search_failed": search_result.failed,
        "skipped_for_cap": len(new_tokens) - len(capped_tokens),
    }

    client = get_client(ats)
    for token in capped_tokens:
        try:
            jobs = client.fetch_jobs(token)
        except (IngestionUnavailable, IngestionParseError):
            logger.exception("Discovery validation failed for token %s (%s)", token, ats)
            stats["failed"] += 1
            continue

        try:
            # A savepoint, not just a try/except: a failed INSERT poisons the
            # enclosing transaction for every later query until rolled back
            # (Postgres aborts the whole transaction on a constraint
            # violation) -- atomic() here means one token's persistence
            # failure can't take the rest of the run down with it.
            with transaction.atomic():
                DiscoveredBoard.objects.create(
                    ats=ats,
                    board_token=token,
                    derived_employer_name=derive_employer_name(ats, token),
                    discovered_job_count=len(jobs),
                )
        except Exception:  # noqa: BLE001 — one token's failure must not abort the rest
            logger.exception("Discovery persistence failed for token %s (%s)", token, ats)
            stats["failed"] += 1
            continue

        stats["validated"] += 1

    return stats
