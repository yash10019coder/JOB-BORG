"""Pure matching scorer.

Takes a profile-criteria snapshot and a job snapshot; returns a weighted score
in [0, 1], the matched tags (the score's explanation), and the threshold-derived
status. No DB access — fully unit-testable and deterministic.

profile snapshot keys: target_titles, target_tags, target_locations,
    target_locations_normalized, excluded_employers, min_salary, remote_pref
job snapshot keys: title, classification_tags, location, location_city,
    location_region, location_country, location_resolved, is_remote,
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


def _hierarchy_match(target, job):
    """Does a resolved profile target hierarchy-match a resolved job location?

    Unset levels on the target are wildcards — only compare levels the user
    actually specified. A target with only ``region`` set (e.g. "California")
    matches any job in that region regardless of city.
    """
    if target["city"] and target["city"] != job.get("location_city"):
        return False
    if target["region"] and target["region"] != job.get("location_region"):
        return False
    if target["country"] and target["country"] != job.get("location_country"):
        return False
    return True


def _substring_fallback(raw_targets, raw_location):
    targets = [loc.lower() for loc in (raw_targets or []) if loc]
    location = (raw_location or "").lower()
    return 1.0 if any(t in location for t in targets) else 0.0


def _match_targets(profile, job):
    targets = profile.get("target_locations_normalized") or []
    if not targets:
        return 1.0  # no location constraint stated — unchanged semantic

    if any(_hierarchy_match(t, job) for t in targets if t["resolved"]):
        return 1.0

    if job.get("location_resolved"):
        # Job is structured; an unresolved/ambiguous target (e.g. bare "NY")
        # must NOT fall back to raw substring against a resolved job — that
        # reintroduces the exact false-positive this scorer exists to fix
        # ("ny" as a substring of "albany"). No hierarchy match on a
        # structured job means no match, full stop.
        return 0.0

    # Job itself isn't in the curated alias table yet (thin coverage) — fall
    # back to the pre-structured substring behavior rather than penalizing
    # users for a curation gap outside their control.
    return _substring_fallback(profile.get("target_locations"), job.get("location"))


def _location_component(profile, job):
    remote_pref = profile.get("remote_pref", Profile.RemotePref.ANY)
    is_remote = bool(job.get("is_remote"))

    if remote_pref == Profile.RemotePref.REMOTE_ONLY:
        return 1.0 if is_remote else 0.0
    if remote_pref == Profile.RemotePref.ONSITE_ONLY:
        if is_remote:
            return 0.0
        return _match_targets(profile, job)

    # ANY: remote satisfies location wholesale; otherwise check target locations.
    if is_remote:
        return 1.0
    return _match_targets(profile, job)


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
