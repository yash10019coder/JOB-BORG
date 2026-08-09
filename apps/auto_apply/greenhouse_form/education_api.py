"""Greenhouse's public per-board "education" reference-data API.

Some Greenhouse combobox fields (School, Degree, Discipline) are backed by
a remote, paginated JSON API rather than a small bundled option list --
confirmed live against a real job posting:
``GET /v1/boards/{board_token}/education/schools?page=N`` returns up to
100 items per page, with ``meta.total_count`` naming the real total (2,466
for that board). The rendered widget only loads as many pages as a real
scroll interaction triggers (see ``client.py``'s ``_extract_options``),
which is bounded/best-effort by design -- this module fetches the
complete list directly instead, when available, since it's both cheaper
(a handful of JSON requests vs. many real-browser scroll-and-wait cycles)
and fully authoritative rather than a partial sample.

Pure HTTP + parsing, no Playwright/browser dependency -- mirrors
``apps.jobs.ingestion.greenhouse_client.GreenhouseClient``'s shape
(session injection for testability, fail-closed on any unexpected
response) for the same reasons: this hits a real third-party API whose
exact failure modes (down, rate-limited, board doesn't have this endpoint
enabled) can't be fully enumerated up front.
"""
from __future__ import annotations

import re

import requests
from django.core.cache import cache

_BASE_URL = "https://boards.greenhouse.io/v1/boards"
# Shared, slow-changing reference data (a list of real-world school/degree
# names) -- not job- or draft-specific, so a long TTL avoids re-paginating
# the same ~25 pages on every single draft for the same board.
_CACHE_TTL_SECONDS = 24 * 60 * 60
_REQUEST_TIMEOUT_S = 5
# Safety backstop against an unbounded loop if `meta.total_count` is ever
# missing/wrong -- real lists observed so far top out at 25 pages (School,
# 2,466 entries at 100/page).
_MAX_PAGES = 30

# Maps a combobox's discovered control_id shape (Greenhouse's standard
# repeatable "Education" block naming: `school--0`, `degree--1`, ...) to
# the API's education-type path segment. `discipline` is included on the
# same naming convention even though no live example has been observed
# yet -- Greenhouse's education block commonly offers School/Degree/
# Discipline together.
_CONTROL_ID_TO_EDUCATION_TYPE = {
    "school": "schools",
    "degree": "degrees",
    "discipline": "disciplines",
}
_CONTROL_ID_PATTERN = re.compile(r"^(school|degree|discipline)--\d+$")


def education_type_for_control_id(control_id: str) -> str | None:
    """Return the API education-type path segment for a `control_id`
    matching Greenhouse's standard Education block naming, or None if it
    doesn't match (i.e. this isn't an API-backed education field)."""
    match = _CONTROL_ID_PATTERN.match(control_id or "")
    if not match:
        return None
    return _CONTROL_ID_TO_EDUCATION_TYPE[match.group(1)]


def fetch_full_list(
    board_token: str, education_type: str, *, session: requests.Session | None = None
) -> tuple[str, ...] | None:
    """Fetch the complete option list for `education_type` on `board_token`
    via Greenhouse's public education API, paginating through every page.

    Returns None (never raises) on any failure -- an unexpected response
    shape, a non-200 status, a network error, or a board without this
    endpoint enabled -- so callers can fall back to whatever was already
    discovered from the live DOM rather than losing the field entirely.
    Cached per (board_token, education_type): see module docstring.
    """
    cache_key = f"greenhouse_education_api:{board_token}:{education_type}"
    cached = cache.get(cache_key)
    if cached is not None:
        return tuple(cached)

    session = session or requests.Session()
    items: list[str] = []
    try:
        page_number = 1
        total_count = None
        while True:
            response = session.get(
                f"{_BASE_URL}/{board_token}/education/{education_type}",
                params={"page": page_number},
                timeout=_REQUEST_TIMEOUT_S,
            )
            if response.status_code != 200:
                return None
            body = response.json()
            page_items = body.get("items")
            if not isinstance(page_items, list):
                return None
            items.extend(
                str(item["text"]).strip()
                for item in page_items
                if isinstance(item, dict) and item.get("text")
            )
            if total_count is None:
                total_count = (body.get("meta") or {}).get("total_count")
            if not page_items or total_count is None or len(items) >= total_count:
                break
            page_number += 1
            if page_number > _MAX_PAGES:
                break
    except Exception:  # noqa: BLE001 -- any failure here just means "no complete list available"
        return None

    result = tuple(dict.fromkeys(items))  # de-dup while preserving order
    cache.set(cache_key, result, timeout=_CACHE_TTL_SECONDS)
    return result
