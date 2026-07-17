"""Map raw Greenhouse job JSON -> a plain dict matching Job model fields.

Employer is deliberately NOT derived here: a job's employer is resolved from
its JobSource FK during upsert (U6), never from the payload's company name.
Greenhouse exposes no structured remote flag or salary, so ``is_remote`` is
derived from the location string here (the one place derivation is allowed —
the matching layer trusts the resulting explicit flag and never re-infers) and
salary fields normalize to None.
"""
import html

from .exceptions import GreenhouseParseError

SOURCE_ATS = "greenhouse"

# Substrings (lowercased) in a location name that mark a posting as remote.
_REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh")


def _derive_is_remote(location_name):
    loc = (location_name or "").lower()
    return any(marker in loc for marker in _REMOTE_MARKERS)


def normalize_job(raw):
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

    return {
        "source_ats": SOURCE_ATS,
        "source_job_id": str(job_id),
        "title": title,
        "description": description,
        "location": location_name,
        "is_remote": _derive_is_remote(location_name),
        "salary_min": None,
        "salary_max": None,
        "source_url": raw.get("absolute_url", ""),
    }
