"""Celery task for re-normalizing locations after an alias-table version bump.

Mirrors apps/classification/tasks.py's sweep_unclassified: a periodic Beat
entry that catches any row not yet at CURRENT_LOCATION_ALIAS_VERSION. There
is no per-row event that triggers this (an alias-table bump is a deploy-time
event, not a per-row one) -- the sweep is the sole re-normalization trigger,
same as ruleset_version bumps rely solely on classification's sweep.

Functionally identical to running the U6 backfill again: both call the same
backfill_jobs/backfill_profiles service functions. This task exists so a
*future* alias-table re-curation has a scheduled, automatic path, not just
the manual management command.
"""
import logging

from celery import shared_task

from apps.accounts.models import Profile
from apps.jobs.models import Job

from .services import backfill_jobs, backfill_profiles

logger = logging.getLogger(__name__)


@shared_task(name="apps.locations.sweep_stale_locations")
def sweep_stale_locations(batch_size=None):
    """Periodic Beat sweep — re-normalize rows stale relative to the current
    alias-table version, a bounded batch at a time."""
    job_stats = backfill_jobs(Job, batch_size=batch_size)
    profile_stats = backfill_profiles(Profile, batch_size=batch_size)
    stats = {"jobs_updated": job_stats["updated"], "profiles_updated": profile_stats["updated"]}
    logger.info("Location alias sweep: %s", stats)
    return stats
