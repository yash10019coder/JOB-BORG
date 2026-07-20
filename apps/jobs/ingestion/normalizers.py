"""Map raw Greenhouse job JSON -> a plain dict matching Job model fields.

Employer is deliberately NOT derived here: a job's employer is resolved from
its JobSource FK during upsert (U6), never from the payload's company name.
Greenhouse exposes no structured remote flag or salary, so ``is_remote`` is
derived from the location string here (the one place derivation is allowed —
the matching layer trusts the resulting explicit flag and never re-infers) and
salary fields normalize to None.
"""
import html

from apps.locations.engine import (
    CURRENT_LOCATION_ALIAS_VERSION,
    REMOTE_MARKERS,
    normalize_location,
)

from .exceptions import AshbyParseError, GreenhouseParseError, LeverParseError

GREENHOUSE_SOURCE_ATS = "greenhouse"
LEVER_SOURCE_ATS = "lever"
ASHBY_SOURCE_ATS = "ashby"
WORKDAY_SOURCE_ATS = "workday"


def _derive_is_remote(location_name):
    loc = (location_name or "").lower()
    return any(marker in loc for marker in REMOTE_MARKERS)


def normalize_greenhouse_job(raw):
    """Return a normalized job dict. Raises GreenhouseParseError on bad shape."""
    if not isinstance(raw, dict):
        raise GreenhouseParseError(
            f"Expected a job object, got {type(raw).__name__}"
        )

    job_id = raw.get("id")
    title = raw.get("title")
    if job_id is None or not title:
        raise GreenhouseParseError(
            "Job is missing a required field (id or title)"
        )

    location_obj = raw.get("location") or {}
    location_name = location_obj.get("name", "") if isinstance(location_obj, dict) else ""

    # content is HTML-entity-escaped in the API; unescape once so keyword rules
    # in classification see real text rather than &lt; noise.
    content = raw.get("content") or ""
    description = html.unescape(content)

    structured_location = normalize_location(location_name)

    return {
        "source_ats": GREENHOUSE_SOURCE_ATS,
        "source_job_id": str(job_id),
        "title": title,
        "description": description,
        "location": location_name,
        "is_remote": _derive_is_remote(location_name),
        "location_city": structured_location["city"] or "",
        "location_region": structured_location["region"] or "",
        "location_country": structured_location["country"] or "",
        "location_resolved": structured_location["resolved"],
        "location_alias_version": CURRENT_LOCATION_ALIAS_VERSION,
        "salary_min": None,
        "salary_max": None,
        "source_url": raw.get("absolute_url", ""),
    }


def normalize_lever_job(raw):
    """Return a normalized job dict. Raises LeverParseError on bad shape.

    Field shape confirmed against a live fetch of
    ``https://api.lever.co/v0/postings/palantir?mode=json`` during
    implementation (see docs/plans/2026-07-21-001-feat-ats-platform-expansion-plan.md).
    Lever exposes no structured salary; those fields normalize to None like
    Greenhouse. Unlike Greenhouse, Lever's ``workplaceType`` is a direct
    remote/hybrid/onsite flag — used as the primary remote signal, with the
    location-text fallback kept for postings where it's absent.
    """
    if not isinstance(raw, dict):
        raise LeverParseError(
            f"Expected a job object, got {type(raw).__name__}"
        )

    job_id = raw.get("id")
    title = raw.get("text")
    if not job_id or not title:
        raise LeverParseError(
            "Job is missing a required field (id or text)"
        )

    categories = raw.get("categories") or {}
    location_name = categories.get("location") or ""

    # descriptionPlain is already unescaped/plain-text (no double-encoding
    # like Greenhouse's content field), so no html.unescape needed here.
    description = raw.get("descriptionPlain") or ""

    structured_location = normalize_location(location_name)

    workplace_type = raw.get("workplaceType")
    is_remote = workplace_type == "remote" or _derive_is_remote(location_name)

    return {
        "source_ats": LEVER_SOURCE_ATS,
        "source_job_id": str(job_id),
        "title": title,
        "description": description,
        "location": location_name,
        "is_remote": is_remote,
        "location_city": structured_location["city"] or "",
        "location_region": structured_location["region"] or "",
        "location_country": structured_location["country"] or "",
        "location_resolved": structured_location["resolved"],
        "location_alias_version": CURRENT_LOCATION_ALIAS_VERSION,
        "salary_min": None,
        "salary_max": None,
        "source_url": raw.get("hostedUrl", ""),
    }


def normalize_ashby_job(raw):
    """Return a normalized job dict. Raises AshbyParseError on bad shape.

    Field shape confirmed against a live fetch of
    ``https://api.ashbyhq.com/posting-api/job-board/ramp`` during
    implementation (see docs/plans/2026-07-21-001-feat-ats-platform-expansion-plan.md).
    Ashby exposes no structured salary in the public job-board API; those
    fields normalize to None like Greenhouse and Lever. ``isRemote`` is a
    direct boolean on the payload — used as the primary remote signal, with
    the location-text fallback kept for consistency with the other
    normalizers.
    """
    if not isinstance(raw, dict):
        raise AshbyParseError(
            f"Expected a job object, got {type(raw).__name__}"
        )

    job_id = raw.get("id")
    title = raw.get("title")
    if not job_id or not title:
        raise AshbyParseError(
            "Job is missing a required field (id or title)"
        )

    location_name = raw.get("location") or ""
    description = raw.get("descriptionPlain") or ""

    structured_location = normalize_location(location_name)

    is_remote = bool(raw.get("isRemote")) or _derive_is_remote(location_name)

    return {
        "source_ats": ASHBY_SOURCE_ATS,
        "source_job_id": str(job_id),
        "title": title,
        "description": description,
        "location": location_name,
        "is_remote": is_remote,
        "location_city": structured_location["city"] or "",
        "location_region": structured_location["region"] or "",
        "location_country": structured_location["country"] or "",
        "location_resolved": structured_location["resolved"],
        "location_alias_version": CURRENT_LOCATION_ALIAS_VERSION,
        "salary_min": None,
        "salary_max": None,
        "source_url": raw.get("jobUrl", ""),
    }


def normalize_workday_job(job):
    """Return a normalized job dict from a vendored ``Job`` pydantic instance.

    Unlike the other normalizers, the input here is already a validated
    pydantic model (``apps.jobs.ingestion.vendor.workday.models.Job``)
    returned by the vendored ``WorkdayScraper.fetch()``, not a raw dict --
    the scraper itself guarantees required fields (``title``, ``ats_id``)
    are always populated (with placeholder fallbacks), so there's no
    dict-shape validation to do here. Workday's public search API exposes no
    structured salary; those fields normalize to None like every other
    platform. ``is_remote`` combines the scraper's own remoteType-derived
    signal with the location-text fallback, same as the other normalizers.
    """
    location_name = job.location or ""
    structured_location = normalize_location(location_name)

    is_remote = bool(job.is_remote) or _derive_is_remote(location_name)

    return {
        "source_ats": WORKDAY_SOURCE_ATS,
        "source_job_id": str(job.ats_id) if job.ats_id else "",
        "title": job.title,
        "description": job.description or "",
        "location": location_name,
        "is_remote": is_remote,
        "location_city": structured_location["city"] or "",
        "location_region": structured_location["region"] or "",
        "location_country": structured_location["country"] or "",
        "location_resolved": structured_location["resolved"],
        "location_alias_version": CURRENT_LOCATION_ALIAS_VERSION,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "source_url": str(job.url) if job.url else "",
    }
