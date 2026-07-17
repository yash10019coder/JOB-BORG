"""Celery tasks for classification.

- ``classify_jobs`` runs event-driven (per U6's enqueue) over specific ids.
- ``sweep_unclassified`` runs on a Beat interval, draining any flagged jobs the
  event path missed — both bounded to a batch so a single invocation stays small.
"""
import logging

from celery import shared_task
from django.conf import settings

from apps.jobs.models import Job

from .engine import load_ruleset
from .services import classify_job

logger = logging.getLogger(__name__)


def enqueue_matching(job_id):
    """Trigger the matching fan-out for a job whose tags changed.

    Sent by name to avoid an import cycle with the matching app (built in U10).
    """
    from config.celery import app

    app.send_task("apps.matching.match_job_to_profiles", kwargs={"job_id": job_id})


def _classify_batch(job_ids=None, batch_size=None):
    batch_size = batch_size or settings.CLASSIFICATION_BATCH_SIZE
    qs = Job.objects.filter(needs_classification=True).order_by("id")
    if job_ids is not None:
        qs = qs.filter(id__in=job_ids)
    batch = list(qs[:batch_size])

    ruleset = load_ruleset()  # load once, reuse across the batch
    classified = 0
    matched = 0
    for job in batch:
        if classify_job(job, ruleset):
            enqueue_matching(job.id)
            matched += 1
        classified += 1

    stats = {"classified": classified, "matching_enqueued": matched}
    logger.info("Classification batch: %s", stats)
    return stats


@shared_task(name="apps.classification.classify_jobs")
def classify_jobs(job_ids=None, batch_size=None):
    """Classify a bounded batch. With ``job_ids``, restrict to those (event-driven)."""
    return _classify_batch(job_ids=job_ids, batch_size=batch_size)


@shared_task(name="apps.classification.sweep_unclassified")
def sweep_unclassified(batch_size=None):
    """Periodic Beat sweep — drain flagged jobs a batch at a time."""
    return _classify_batch(job_ids=None, batch_size=batch_size)
