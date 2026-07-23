"""Location normalization engine: resolve a free-text location string into a
structured city/region/country dict, using a curated, versioned YAML dataset.

Mirrors ``apps/classification/engine.py``'s versioned-static-data pattern:
a dataset file is loaded once (``lru_cache``) and matched against with pure,
deterministic, side-effect-free logic. No network calls, no DB access.

This app is a dependency-free leaf: it is imported by ``apps/jobs/ingestion``
and ``apps/web/forms.py``, never the reverse, so it must not import from
``apps.jobs``, ``apps.accounts``, or ``apps.matching``.
"""
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

GEODATA_DIR = Path(__file__).resolve().parent / "geodata"

# Applied when a new curated vN.yaml is promoted, OR when a change to this
# file's resolution logic itself could change a location's structured
# output — the version stamp is the only signal the sweep task uses to find
# rows that need re-normalizing, so a logic-only fix with no version bump
# leaves existing rows silently stale.
CURRENT_LOCATION_ALIAS_VERSION = "v1"

# Substrings (lowercased) that mark a posting as remote. Public so
# apps/jobs/ingestion/normalizers.py's is_remote derivation can reuse the
# exact same vocabulary instead of hand-maintaining a duplicate copy --
# apps.jobs already depends on apps.locations (the reverse would violate the
# leaf-app rule), so this direction of reuse is safe.
REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh")

_MULTI_LOCATION_DELIMITERS = (" or ", "/")

_UNRESOLVED = {"city": None, "region": None, "country": None, "resolved": False}

# GeoNames' `feature code` column, tiered by administrative significance --
# more stable than population alone for same-type city disambiguation
# (population has documented staleness/duplicate-row issues across GeoNames
# refreshes; feature code does not drift the same way). Lower tier wins.
_FEATURE_CODE_TIER = {
    "PPLC": 0,  # capital of a political entity
    "PPLA": 1,  # seat of a first-order admin division
    "PPLA2": 2,
    "PPLA3": 3,
    "PPLA4": 4,
    "PPLA5": 5,
}
_DEFAULT_FEATURE_CODE_TIER = 9


def feature_code_tier(feature_code):
    """Lower is more significant. Unknown/plain/missing codes sort last."""
    return _FEATURE_CODE_TIER.get(feature_code, _DEFAULT_FEATURE_CODE_TIER)


def _best_city_candidate(matches):
    """Same-type tiebreak: highest feature-code tier, then highest population."""
    return min(
        matches,
        key=lambda m: (feature_code_tier(m.get("feature_code")), -(m.get("population") or 0)),
    )


class LocationDataError(Exception):
    """Raised when the curated dataset file is missing or malformed."""


class _GeoIndex:
    """Lookup tables built once from the loaded YAML dataset."""

    def __init__(self, data):
        self.country_by_alias = {}
        self.region_full_by_alias = {}
        self.region_any_by_alias = {}
        self.region_scoped_by_country_alias = {}
        self.city_by_alias = {}
        self.ambiguous_bare_tokens = set(data.get("ambiguous_bare_tokens") or [])

        for country in data.get("countries") or []:
            name = country["name"]
            for alias in country.get("aliases") or []:
                self.country_by_alias[alias] = name

        for region in data.get("regions") or []:
            code = region["code"]
            country = region["country"]
            for alias in region.get("full_aliases") or []:
                self.region_full_by_alias[alias] = (code, country)
                self.region_any_by_alias[alias] = (code, country)
                self.region_scoped_by_country_alias[(country, alias)] = code
            for alias in region.get("abbrev_aliases") or []:
                self.region_any_by_alias[alias] = (code, country)
                self.region_scoped_by_country_alias[(country, alias)] = code

        for city in data.get("cities") or []:
            entry = {
                "name": city["name"],
                "region": city.get("region"),
                "country": city["country"],
                "population": city.get("population"),
                "feature_code": city.get("feature_code"),
            }
            for alias in city.get("aliases") or []:
                self.city_by_alias.setdefault(alias, []).append(entry)


@lru_cache(maxsize=None)
def _load_index(version=CURRENT_LOCATION_ALIAS_VERSION):
    path = GEODATA_DIR / f"{version}.yaml"
    if not path.exists():
        raise LocationDataError(f"No location dataset file for version {version!r}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise LocationDataError(f"Location dataset {version!r} is malformed")
    return _GeoIndex(data)


def _clean(raw):
    if not raw or not isinstance(raw, str):
        return ""
    s = unicodedata.normalize("NFKC", raw)
    s = s.strip().casefold()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .,-")
    return s


def _first_multi_location_segment(cleaned):
    for delim in _MULTI_LOCATION_DELIMITERS:
        if delim in cleaned:
            return cleaned.split(delim, 1)[0].strip(" .,-")
    return cleaned


def _strip_remote_markers(cleaned):
    result = cleaned
    for marker in REMOTE_MARKERS:
        result = result.replace(marker, " ")
    result = re.sub(r"[\s\-–—]+", " ", result).strip(" -–—,.")
    return result


def _split_segments(remainder):
    segments = [seg.strip(" .,-") for seg in remainder.split(",")]
    return [seg for seg in segments if seg]


def _resolve_bare(token, index):
    if token in index.ambiguous_bare_tokens:
        return dict(_UNRESOLVED)
    country = index.country_by_alias.get(token)
    if country:
        return {"city": None, "region": None, "country": country, "resolved": True}
    region = index.region_full_by_alias.get(token)
    if region:
        code, country = region
        return {"city": None, "region": code, "country": country, "resolved": True}
    matches = index.city_by_alias.get(token)
    if matches:
        # Same-type collision (e.g. multiple cities named "Springfield"):
        # resolve via feature-code tier then population rather than staying
        # unresolved -- the one city type with a reliable secondary signal.
        # Cross-type collisions never reach here; they're caught by the
        # ambiguous_bare_tokens check above (see geodata_generation.py).
        m = _best_city_candidate(matches)
        return {"city": m["name"], "region": m["region"], "country": m["country"], "resolved": True}
    return dict(_UNRESOLVED)


def _resolve_segments(segments, index):
    if not segments:
        return dict(_UNRESOLVED)
    if len(segments) == 1:
        return _resolve_bare(segments[0], index)

    *head, tail = segments
    country = index.country_by_alias.get(tail)
    region = None
    if country is None:
        region_match = index.region_any_by_alias.get(tail)
        if region_match:
            region, country = region_match

    if country is None and region is None:
        # The tail segment is always present here (len(segments) >= 2), but it
        # didn't resolve to anything curated. Falling through to an
        # unconstrained head-only city match would silently discard the tail
        # and confidently resolve garbage like "Austin, Georgia" to Austin,
        # TX, US -- the exact class of confidently-wrong match this dataset
        # exists to prevent. An unrecognized tail means the whole entry stays
        # unresolved, not "trust the city alone."
        return dict(_UNRESOLVED)

    if region is None and head:
        candidate = head[-1]
        scoped = index.region_scoped_by_country_alias.get((country, candidate))
        if scoped:
            region = scoped

    city = None
    if head:
        candidate = head[0]
        matches = index.city_by_alias.get(candidate)
        if matches:
            narrowed = [
                m for m in matches
                if m["country"] == country
                and (region is None or m["region"] == region)
            ]
            if len(narrowed) == 1:
                city = narrowed[0]

    return {
        "city": city["name"] if city else None,
        "region": region,
        "country": country,
        "resolved": True,
    }


def normalize_location(raw):
    """Resolve a free-text location string into a structured dict.

    Returns ``{"city": str|None, "region": str|None, "country": str|None,
    "resolved": bool}``. Never raises on ``None``, empty, or malformed input
    — mirrors ``apps/jobs/ingestion/normalizers._derive_is_remote``'s
    never-raise contract, since ingestion and profile-save both call this
    unconditionally on user- or scraper-supplied text.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return dict(_UNRESOLVED)

    first_segment = _first_multi_location_segment(cleaned)
    remainder = _strip_remote_markers(first_segment)
    if not remainder:
        return dict(_UNRESOLVED)

    segments = _split_segments(remainder)
    try:
        index = _load_index()
    except LocationDataError:
        # Honor the never-raise contract even when the curated dataset itself
        # is missing or malformed -- every call site (ingestion, profile
        # save, the backfill/sweep loop) calls this unconditionally and does
        # not expect an exception.
        logger.error("Location dataset failed to load; treating input as unresolved", exc_info=True)
        return dict(_UNRESOLVED)
    return _resolve_segments(segments, index)
