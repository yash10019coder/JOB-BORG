"""Celery tasks for the matching fan-out.

- ``match_job_to_profiles`` — job-centric, triggered when a job's tags change.
- ``rematch_profile`` — profile-centric, triggered (debounced) by profile edits.
"""
import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

from apps.accounts.models import Profile
from apps.jobs.models import Job

from .services import match_job, rematch_profile_obj

logger = logging.getLogger(__name__)


def _rematch_token_key(profile_id):
    return f"rematch_token:{profile_id}"


def schedule_rematch(profile_id):
    """Debounced enqueue: rapid successive saves collapse to one execution.

    Each call mints a fresh token stored in the shared cache and schedules the
    task after a short delay. When the delayed task fires it runs only if its
    token is still the latest — earlier tasks find a newer token and no-op.
    """
    token = uuid.uuid4().hex
    cache.set(_rematch_token_key(profile_id), token, timeout=3600)
    rematch_profile.apply_async(
        kwargs={"profile_id": profile_id, "token": token},
        countdown=settings.REMATCH_DEBOUNCE_SECONDS,
    )


@shared_task(name="apps.matching.match_job_to_profiles")
def match_job_to_profiles(job_id):
    try:
        job = Job.objects.select_related("employer").get(pk=job_id)
    except Job.DoesNotExist:
        logger.warning("match_job_to_profiles: job %s no longer exists", job_id)
        return {"job_id": job_id, "missing": True}
    stats = match_job(job)
    logger.info("match_job_to_profiles %s: %s", job_id, stats)
    return stats


@shared_task(name="apps.matching.rematch_profile")
def rematch_profile(profile_id, token=None):
    # Debounce: skip if a newer save has superseded this scheduled run.
    if token is not None:
        current = cache.get(_rematch_token_key(profile_id))
        if current != token:
            return {"profile_id": profile_id, "skipped": True}

    try:
        profile = Profile.objects.select_related("user").get(pk=profile_id)
    except Profile.DoesNotExist:
        logger.warning("rematch_profile: profile %s no longer exists", profile_id)
        return {"profile_id": profile_id, "missing": True}
    stats = rematch_profile_obj(profile)
    logger.info("rematch_profile %s: %s", profile_id, stats)
    return stats
