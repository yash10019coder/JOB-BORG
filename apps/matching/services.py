"""Matching fan-out services — the multi-user scoring layer.

Two entry points share this logic:
- ``match_job`` (job-centric): one newly-classified/updated job vs. all active
  profiles, DB-pre-filtered to a candidate set before scoring.
- ``rematch_profile_obj`` (profile-centric): one edited profile vs. a recent
  window of open jobs.

Disqualification (a profile narrowed past a job, or a job closed) is handled by
deleting the UserJobMatch row — the job simply leaves that user's list. Any
JobApplication the user saved lives in a separate table and is never touched.
"""
import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Profile
from apps.jobs.models import Job

from .models import UserJobMatch
from .prefilter import passes_prefilter
from .scoring import score_job


def profile_snapshot(profile):
    return {
        "target_titles": profile.target_titles,
        "target_tags": profile.target_tags,
        "target_locations": profile.target_locations,
        "target_locations_normalized": profile.target_locations_normalized,
        "excluded_employers": profile.excluded_employers,
        "min_salary": profile.min_salary,
        "remote_pref": profile.remote_pref,
    }


def job_snapshot(job):
    return {
        "title": job.title,
        "classification_tags": job.classification_tags,
        "location": job.location,
        "location_city": job.location_city,
        "location_region": job.location_region,
        "location_country": job.location_country,
        "location_resolved": job.location_resolved,
        "is_remote": job.is_remote,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "employer_slug": job.employer.slug,
    }


def candidate_profiles_for_job(job):
    """DB-level pre-filter: active profiles that could match THIS job.

    Uses the job's own (indexed) column values to exclude profiles up front, so
    scoring never runs O(jobs x users). Mirrors ``passes_prefilter`` in SQL.
    """
    qs = Profile.objects.filter(is_active=True).select_related("user")

    # Remote preference vs. this job's remote flag.
    if job.is_remote:
        qs = qs.exclude(remote_pref=Profile.RemotePref.ONSITE_ONLY)
    else:
        qs = qs.exclude(remote_pref=Profile.RemotePref.REMOTE_ONLY)

    # Excluded-employer: profiles listing this job's employer slug.
    qs = qs.exclude(excluded_employers__contains=[job.employer.slug])

    # Salary floor: exclude profiles whose floor exceeds a KNOWN job ceiling.
    ceiling = job.salary_max if job.salary_max is not None else job.salary_min
    if ceiling is not None:
        qs = qs.exclude(min_salary__gt=ceiling)

    return qs


def _build_match(profile, job, js):
    result = score_job(profile_snapshot(profile), js)
    return UserJobMatch(
        user_id=profile.user_id,
        job=job,
        match_score=result.score,
        match_status=result.status,
        matched_tags=result.matched_tags,
        computed_at=timezone.now(),
    )


def _bulk_upsert(matches):
    if not matches:
        return
    UserJobMatch.objects.bulk_create(
        matches,
        update_conflicts=True,
        unique_fields=["user", "job"],
        update_fields=["match_score", "match_status", "matched_tags", "computed_at"],
        batch_size=settings.MATCH_BULK_BATCH_SIZE,
    )


@transaction.atomic
def match_job(job):
    """Job-centric fan-out. Returns a stats dict."""
    # A closed job disqualifies every match — remove them and stop.
    if job.status != Job.Status.OPEN:
        deleted, _ = UserJobMatch.objects.filter(job=job).delete()
        return {"job_id": job.id, "closed": True, "deleted": deleted}

    candidates = list(candidate_profiles_for_job(job))
    candidate_user_ids = [p.user_id for p in candidates]

    # Drop stale matches on this job for users who are no longer candidates
    # (e.g. the job flipped to onsite and a remote-only user no longer fits).
    UserJobMatch.objects.filter(job=job).exclude(
        user_id__in=candidate_user_ids
    ).delete()

    js = job_snapshot(job)
    matches = [_build_match(p, job, js) for p in candidates]
    _bulk_upsert(matches)
    return {"job_id": job.id, "scored": len(matches)}


@transaction.atomic
def rematch_profile_obj(profile):
    """Profile-centric fan-out over a recent window of open jobs. Returns stats."""
    # Inactive profiles participate in nothing — clear any existing matches.
    if not profile.is_active:
        deleted, _ = UserJobMatch.objects.filter(user_id=profile.user_id).delete()
        return {"profile_id": profile.id, "inactive": True, "deleted": deleted}

    window_start = timezone.now() - datetime.timedelta(
        days=settings.REMATCH_JOB_WINDOW_DAYS
    )
    jobs = (
        Job.objects.filter(status=Job.Status.OPEN, scraped_at__gte=window_start)
        .select_related("employer")
    )

    ps = profile_snapshot(profile)
    upserts = []
    disqualified_job_ids = []
    for job in jobs:
        js = job_snapshot(job)
        if passes_prefilter(ps, js):
            result = score_job(ps, js)
            upserts.append(
                UserJobMatch(
                    user_id=profile.user_id,
                    job=job,
                    match_score=result.score,
                    match_status=result.status,
                    matched_tags=result.matched_tags,
                    computed_at=timezone.now(),
                )
            )
        else:
            disqualified_job_ids.append(job.id)

    # Narrowing: delete this user's matches on jobs that no longer qualify.
    if disqualified_job_ids:
        UserJobMatch.objects.filter(
            user_id=profile.user_id, job_id__in=disqualified_job_ids
        ).delete()

    # Also clear matches on jobs that have since closed (belt-and-braces with U6).
    UserJobMatch.objects.filter(
        user_id=profile.user_id, job__status=Job.Status.CLOSED
    ).delete()

    _bulk_upsert(upserts)
    return {"profile_id": profile.id, "scored": len(upserts)}
