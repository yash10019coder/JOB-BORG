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
CURRENT_LOCATION_ALIAS_VERSION = "v2"

# Substrings (lowercased) that mark a posting as remote. Public so
# apps/jobs/ingestion/normalizers.py's is_remote derivation can reuse the
# exact same vocabulary instead of hand-maintaining a duplicate copy --
# apps.jobs already depends on apps.locations (the reverse would violate the
# leaf-app rule), so this direction of reuse is safe.
REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "world wide")

_MULTI_LOCATION_DELIMITERS = (" or ", "/")

_UNRESOLVED = {"city": None, "region": None, "country": None, "resolved": False}

# A defined "no place information, and that's fine" state -- distinct from
# _UNRESOLVED, which means "there's a place here the dataset hasn't curated
# yet." A bare remote/hybrid string with nothing left after marker-stripping
# has nothing a curator could add, so it shouldn't count as a coverage gap
# (see apps/jobs/admin.py's location_resolved filter).
_NO_PLACE_INFO = {"city": None, "region": None, "country": None, "resolved": True}

# R7: a trailing "<City> Area" suffix (LinkedIn-style), stripped before place
# matching. Anchored at the end so it can't interfere with R8's start-anchored
# prefix stripping.
_AREA_SUFFIX_RE = re.compile(r"\s+area$")

# R8: a leading two-letter country-code prefix + separator (e.g.
# "SG - Singapore", "UK - London"). Anchored at the start. Matched against
# the loaded index's actual country aliases (not blindly any two letters) so
# it doesn't fire on non-country two-letter tokens -- though a code that's
# also a common US state abbreviation (e.g. ISO "IN" = India vs. Indiana) is
# a known, deferred edge case (see plan Open Questions), not evidenced in
# real data as of this dataset.
_TWO_LETTER_PREFIX_RE = re.compile(r"^([a-z]{2})\s*-\s*(.+)$")

# GeoNames' `feature code` column, tiered by administrative significance.
# Used as the *secondary* same-type tiebreak, after population -- see
# _best_city_candidate for why. Lower tier wins.
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
    """Same-type tiebreak: highest population, then highest feature-code tier.

    Population leads (reversed from the plan's original feature-code-first
    design) based on real spot-check evidence against the generated v2
    dataset: a bare "San Francisco" lookup has 8 same-type candidates
    worldwide, and feature-code-first picked San Francisco, El Salvador
    (population 16,152, admin tier 1 -- a department seat) over San
    Francisco, California (population 827,526, admin tier 2 -- not its
    state capital). Feature-code tier reflects within-country administrative
    rank, not global prominence, so it can rank a small foreign admin seat
    above a much larger, far more likely-intended city. Population is the
    more direct proxy for "which real place would a job poster most likely
    mean by writing just the bare name" -- feature-code tier remains the
    secondary tiebreak for the genuine population-tie case.
    """
    return max(
        matches,
        key=lambda m: (m.get("population") or 0, -feature_code_tier(m.get("feature_code"))),
    )


class LocationDataError(Exception):
    """Raised when the curated dataset file is missing or malformed."""


class _GeoIndex:
    """Lookup tables built once from the loaded YAML dataset."""

    def __init__(self, data):
        self.country_by_alias = {}
        self.country_population = {}
        self.region_full_by_alias = {}
        # List-valued (unlike region_full_by_alias): same-abbrev collisions
        # across countries are common in real GeoNames admin1 data (e.g.
        # "CA" is both California's postal code and Luxembourg's Capellen
        # district code) and are resolved at lookup time by a
        # country-population tiebreak (see _resolve_segments), not excluded
        # at generation time -- unlike region_full_by_alias, whose bare
        # resolution has no comparable tiebreak signal and fails closed.
        self.region_any_by_alias = {}
        self.region_scoped_by_country_alias = {}
        self.city_by_alias = {}
        self.ambiguous_bare_tokens = set(data.get("ambiguous_bare_tokens") or [])

        for country in data.get("countries") or []:
            name = country["name"]
            self.country_population[name] = country.get("population")
            for alias in country.get("aliases") or []:
                self.country_by_alias[alias] = name

        for region in data.get("regions") or []:
            code = region["code"]
            country = region["country"]
            # full_aliases also register in region_full_by_alias (bare
            # resolution); comma_context_full_aliases and abbrev_aliases
            # never do -- comma_context_full_aliases exists specifically
            # because a region-vs-city same-name collision (e.g.
            # "Washington" the state vs. Washington, D.C.) demoted the bare
            # claim so the city wins there, while the comma-qualified
            # pattern ("Seattle, Washington") has no such collision and
            # must keep resolving via region_any_by_alias.
            for alias in region.get("full_aliases") or []:
                self._register_region_alias(alias, code, country, bare_resolvable=True)
            for alias in region.get("comma_context_full_aliases") or []:
                self._register_region_alias(alias, code, country)
            for alias in region.get("abbrev_aliases") or []:
                self._register_region_alias(alias, code, country)

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

    def _register_region_alias(self, alias, code, country, *, bare_resolvable=False):
        if bare_resolvable:
            self.region_full_by_alias[alias] = (code, country)
        self.region_any_by_alias.setdefault(alias, []).append((code, country))
        self.region_scoped_by_country_alias[(country, alias)] = code


@lru_cache(maxsize=None)
def _load_index(version=CURRENT_LOCATION_ALIAS_VERSION):
    path = GEODATA_DIR / f"{version}.yaml"
    if not path.exists():
        raise LocationDataError(f"No location dataset file for version {version!r}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise LocationDataError(f"Location dataset {version!r} is malformed")
    return _GeoIndex(data)


def _try_load_index():
    """``_load_index()``, honoring normalize_location's never-raise contract.

    Returns ``None`` (and logs) on a missing/malformed dataset rather than
    raising -- every call site treats that the same way (unresolved).
    """
    try:
        return _load_index()
    except LocationDataError:
        logger.error("Location dataset failed to load; treating input as unresolved", exc_info=True)
        return None


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


def _strip_area_suffix(segment):
    return _AREA_SUFFIX_RE.sub("", segment)


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
        region_matches = index.region_any_by_alias.get(tail)
        if region_matches:
            # Same-abbrev collisions across countries are common (e.g. "CA"
            # is both California's postal code and Luxembourg's Capellen
            # district code) -- prefer the region belonging to the more
            # populous country, the same "which one did they likely mean"
            # signal used for the same-type city tiebreak.
            region, country = max(
                region_matches, key=lambda m: index.country_population.get(m[1]) or 0
            )

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
    # R7/R8 run before R9's dash-collapsing _strip_remote_markers, on the
    # not-yet-dash-collapsed segment -- both are anchored (end/start) and
    # don't interact with each other, but _strip_remote_markers' `[\s\-–—]+`
    # collapse would otherwise destroy the " - " delimiter R8 needs to
    # recognize a prefix at all (see plan Key Technical Decisions).
    without_suffix = _strip_area_suffix(first_segment)

    # The dataset is only needed to validate an actual prefix match or to
    # resolve a real place segment -- a bare remote/hybrid string (a large
    # fraction of real job postings) never reaches either, so it shouldn't
    # have to pay for a dataset load at all.
    index = None
    prefix_match = _TWO_LETTER_PREFIX_RE.match(without_suffix)
    if prefix_match:
        index = _try_load_index()
        if index is None:
            return dict(_UNRESOLVED)
        code, remainder_after_prefix = prefix_match.groups()
        without_prefix = (
            remainder_after_prefix.strip() if code in index.country_by_alias else without_suffix
        )
    else:
        without_prefix = without_suffix

    remainder = _strip_remote_markers(without_prefix)
    if not remainder:
        # R9: nothing left after remote-marker stripping means the input was
        # remote/hybrid noise with no place information -- a defined
        # "resolved, no location" state, not a coverage gap to flag.
        return dict(_NO_PLACE_INFO)

    if index is None:
        index = _try_load_index()
        if index is None:
            return dict(_UNRESOLVED)

    segments = _split_segments(remainder)
    return _resolve_segments(segments, index)
