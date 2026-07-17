"""Pure matching scorer.

Takes a profile-criteria snapshot and a job snapshot; returns a weighted score
in [0, 1], the matched tags (the score's explanation), and the threshold-derived
status. No DB access — fully unit-testable and deterministic.

profile snapshot keys: target_titles, target_tags, target_locations,
    excluded_employers, min_salary, remote_pref
job snapshot keys: title, classification_tags, location, is_remote,
    salary_min, salary_max, employer_slug
"""
from dataclasses import dataclass

from apps.accounts.models import Profile

from .constants import (
    LOCATION_WEIGHT,
    SALARY_WEIGHT,
    TAG_WEIGHT,
    TITLE_WEIGHT,
    status_for_score,
)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    matched_tags: list
    status: str


def _matched_tags(profile, job):
    """Intersection of the profile's target tags and the job's tags (sorted).

    Exactly the tags that drove the score — never a tag absent from the job.
    """
    targets = profile.get("target_tags") or []
    job_tags = set(job.get("classification_tags") or [])
    # Preserve determinism and avoid dupes; keep only tags the job actually has.
    seen = []
    for tag in targets:
        if tag in job_tags and tag not in seen:
            seen.append(tag)
    return sorted(seen)


def _tag_component(profile, job, matched):
    targets = profile.get("target_tags") or []
    if not targets:
        return 0.0
    return len(matched) / len(targets)


def _title_component(profile, job):
    targets = [t.lower() for t in (profile.get("target_titles") or [])]
    if not targets:
        return 0.0
    title = (job.get("title") or "").lower()
    for target in targets:
        if target and (target in title or title in target):
            return 1.0
    return 0.0


def _location_component(profile, job):
    remote_pref = profile.get("remote_pref", Profile.RemotePref.ANY)
    is_remote = bool(job.get("is_remote"))

    if remote_pref == Profile.RemotePref.REMOTE_ONLY:
        return 1.0 if is_remote else 0.0
    if remote_pref == Profile.RemotePref.ONSITE_ONLY:
        return 0.0 if is_remote else 1.0

    # ANY: remote satisfies location wholesale; otherwise check target locations.
    if is_remote:
        return 1.0
    targets = [loc.lower() for loc in (profile.get("target_locations") or [])]
    if not targets:
        return 1.0  # no location constraint stated
    location = (job.get("location") or "").lower()
    return 1.0 if any(t and t in location for t in targets) else 0.0


def _salary_component(profile, job):
    min_salary = profile.get("min_salary")
    if min_salary is None:
        return 1.0  # no floor stated
    job_min = job.get("salary_min")
    if job_min is None:
        return 0.5  # unknown salary — neutral, never silently dropped
    return 1.0 if job_min >= min_salary else 0.0


def score_job(profile, job):
    """Return a ScoreResult for a (profile, job) snapshot pair."""
    matched = _matched_tags(profile, job)
    total = (
        TAG_WEIGHT * _tag_component(profile, job, matched)
        + TITLE_WEIGHT * _title_component(profile, job)
        + LOCATION_WEIGHT * _location_component(profile, job)
        + SALARY_WEIGHT * _salary_component(profile, job)
    )
    total = round(total, 6)
    return ScoreResult(score=total, matched_tags=matched, status=status_for_score(total))
