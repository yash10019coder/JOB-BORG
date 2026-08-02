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
CURRENT_LOCATION_ALIAS_VERSION = "v3"

# Substrings (lowercased) that mark a posting as remote. Public so
# apps/jobs/ingestion/normalizers.py's is_remote derivation can reuse the
# exact same vocabulary instead of hand-maintaining a duplicate copy --
# apps.jobs already depends on apps.locations (the reverse would violate the
# leaf-app rule), so this direction of reuse is safe.
REMOTE_MARKERS = (
    "remote", "anywhere", "work from home", "wfh", "world wide", "worldwide",
    "remoto",
)

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

# A "metro"/"metropolitan"/"metropolitain" (French spelling, seen verbatim
# in real production data -- "Rome Metropolitain Area", 47 rows) qualifier
# directly before "Area", stripped only once "Area" itself is already gone
# so a bare "Metro Area" (no city) is untouched -- that string must keep
# reducing to the single generic token "metro" that _GENERIC_AREA_DESCRIPTORS
# blocks, not to "" (which would misclassify it as no-place-info instead of
# an unresolved gap).
_METRO_QUALIFIER_RE = re.compile(r"\s+metro(?:politan|politain)?$")

# Generic geographic descriptors that coincidentally, uniquely match a real
# (obscure) place somewhere in the dataset once R7 strips "Area" off them --
# e.g. "Delta" is a small town in British Columbia, "Metro" a town in
# Indonesia. A unique dataset match isn't a reliable "which real place did
# they mean" signal for a common English word the way it is for an actual
# place name, so these are suppressed from suffix-stripped bare resolution
# regardless of match count.
_GENERIC_AREA_DESCRIPTORS = {
    "bay", "metro", "valley", "delta", "north", "south", "east", "west",
    "central", "greater", "downtown", "uptown", "tri-state",
    "shore", "piedmont", "gold coast", "highlands", "north shore",
}

# R8: a leading two-letter country-code prefix + separator (e.g.
# "SG - Singapore", "UK - London"). Anchored at the start. Matched against
# the loaded index's dedicated country_by_prefix_code lookup (not blindly any
# two letters) so it doesn't fire on non-country two-letter tokens. A code
# that's also a common US state abbreviation (e.g. ISO "GA" = Gabon vs.
# Georgia) is excluded from that lookup entirely, so it correctly stays
# unresolved rather than confidently resolving to the wrong country -- see
# apps/locations/geodata_generation.py's US_STATE_POSTAL_CODES.
_TWO_LETTER_PREFIX_RE = re.compile(r"^([a-z]{2})\s*-\s*(.+)$")

# The mirror of R8, at the tail instead of the head, with no separator at
# all (e.g. "Atlanta GA", "Tempe AZ") -- a common bare "<City> <ST>" shape
# with neither a comma nor a dash. Only tried as a fallback after the plain
# bare-token lookup already failed (see _resolve_segments), so it can never
# turn an already-resolvable single-word city into something else -- it
# only ever gives an otherwise-unresolved string a second chance by
# re-trying it as a (head, tail) pair through the same tail-is-country-or-
# region machinery R8/comma-tail already use, which harmlessly leaves it
# unresolved if the trailing token isn't a real country/region alias (e.g.
# "Emeryville HQ" -- "hq" matches nothing).
_TRAILING_BARE_CODE_RE = re.compile(r"^(.+)\s+([a-z]{2})$")

# A trailing US ZIP code on a comma-tail segment (e.g. "New York, NY
# 10065") -- see _resolve_segments for why. The ZIP+4 separator is matched
# as whitespace, not a literal "-", because this runs after
# _strip_remote_markers has already collapsed every hyphen in the string
# to a space ("10065-1234" -> "10065 1234") by the time the tail segment
# is examined.
_TRAILING_ZIP_RE = re.compile(r"\s+\d{5}(?:\s?\d{4})?$")

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
        # R8's dedicated country-code-prefix lookup, always populated for
        # every country's ISO code (except the ones colliding with a US
        # state postal abbreviation) -- independent of country_by_alias,
        # which drops a code entirely on ANY region-abbrev collision
        # worldwide (see apps/locations/geodata_generation.py's
        # country_iso2_prefixes for why the two lookups can't be merged).
        self.country_by_prefix_code = dict(data.get("country_iso2_prefixes") or {})
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
        # Abbrev-only subset of region_any_by_alias (excludes
        # comma_context_full_aliases, e.g. "new york" the demoted state
        # name) -- region_any_by_alias's comma-context entries are only
        # safe in the tail position of an explicit multi-segment string
        # ("Seattle, Washington"), never for a fully bare single-token
        # lookup, where they'd silently resolve "New York"/"Washington" to
        # the state instead of letting the city win as the demotion
        # intends. Confirmed as a real regression caught by this file's own
        # test suite when a bare-abbrev fallback was added to _resolve_bare
        # using region_any_by_alias directly.
        self.region_abbrev_by_alias = {}
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
                self.region_abbrev_by_alias.setdefault(alias, []).append((code, country))

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
    try:
        return _GeoIndex(data)
    except (KeyError, TypeError, ValueError) as exc:
        # A structurally-malformed dataset (e.g. a country/region/city entry
        # missing a required key) would otherwise raise an uncaught
        # exception here, violating normalize_location's documented
        # never-raise contract -- _try_load_index only catches
        # LocationDataError, so any load failure must surface as one.
        raise LocationDataError(
            f"Location dataset {version!r} is structurally malformed: {exc}"
        ) from exc


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
    # Abbreviation periods ("D.C.", "N.Y.", "U.S. - Chicago") never carry
    # alias-matching significance -- GeoNames-derived aliases are period-
    # free (region abbreviations especially: "dc", never "d.c."), so a
    # period-preserving input silently fails to match its own dataset's
    # alias. Confirmed as a real gap: "Washington, D.C." stayed unresolved
    # while "Washington, DC" already worked. Deleting outright (not just
    # collapsing to a space) so "D.C." reduces to "dc" and matches R8's
    # 2-letter-prefix regex the same way a hand-typed "DC" would.
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .,-")
    return s


def _first_multi_location_segment(cleaned):
    for delim in _MULTI_LOCATION_DELIMITERS:
        if delim in cleaned:
            return cleaned.split(delim, 1)[0].strip(" .,-")
    return cleaned


def _strip_area_suffix(segment):
    without_area = _AREA_SUFFIX_RE.sub("", segment)
    if without_area == segment:
        return segment
    without_metro = _METRO_QUALIFIER_RE.sub("", without_area)
    return without_metro if without_metro else without_area


def _strip_remote_markers(cleaned):
    result = cleaned
    for marker in REMOTE_MARKERS:
        result = result.replace(marker, " ")
    result = re.sub(r"[\s\-–—]+", " ", result).strip(" -–—,.")
    return result


# Words that describe a work arrangement or generic office/entity
# reference, never part of a real place name -- deliberately separate from
# REMOTE_MARKERS (which apps/jobs/ingestion/normalizers.py's is_remote
# derivation also reads: "hybrid", "office", "hq" etc. do NOT imply remote
# and must never be added there). Stripping these serves two purposes: (1)
# unlocks a real place hiding behind office/company noise -- "New York
# Office" -> "New York" resolves, as does "Emeryville HQ" if the city
# itself is in the dataset -- and (2) lets a string with nothing else left
# ("Hybrid", "Location TBD", "2 Locations") correctly fall into the
# existing _NO_PLACE_INFO state below instead of being flagged as an
# unresolved coverage gap when there was never any place info to find.
_LOCATION_NOISE_WORDS_RE = re.compile(
    # "in-office"/"on-site" are written here as space-separated ("in
    # office", "on site") because this runs after _strip_remote_markers,
    # which has already collapsed every hyphen to a space -- a literal
    # "in-office" alternative would never match the text this actually
    # sees. Deliberately excludes "based"/"remotely": REMOTE_MARKERS'
    # substring-replace already turns "remotely" into the fragment "ly"
    # (it strips "remote" out of the middle, leaving "ly"), and stripping
    # "based" too would leave that bare "ly" as the sole remaining token --
    # which then confidently (and wrongly) resolves as Libya's ISO code.
    # Confirmed as a real bug caught by this file's own test suite.
    r"\b(?:\d+\s*locations?|in\s+office|on\s+site|onsite|headquarters|hq|"
    r"office|offices|location|hybrid|distributed|tbd|global|nationwide|"
    r"n/a|multiple\s+(?:cities|locations)|home\s+based|emea|apac|americas|"
    r"latin\s+america|north\s+america|europe|any|ceo)\b"
)


def _strip_location_noise_words(cleaned):
    result = _LOCATION_NOISE_WORDS_RE.sub(" ", cleaned)
    result = re.sub(r"\s+", " ", result).strip(" -–—,.")
    return result


def _split_segments(remainder):
    if " > " in remainder:
        # Breadcrumb format (e.g. "US > Arizona > Phoenix", "China >
        # Shanghai") used by some ATSes -- biggest-to-smallest, the
        # opposite order of the comma convention this module otherwise
        # assumes (city, ..., country). Reversing lets it reuse
        # _resolve_segments' existing tail-is-country/head-is-city logic
        # unchanged. Confirmed as a real, isolated pattern: 45 distinct
        # unresolved production strings, all this exact hierarchy, no
        # comma-delimited variant observed mixing the two conventions.
        segments = [seg.strip(" .,-") for seg in remainder.split(" > ")]
        segments = [seg for seg in segments if seg]
        segments.reverse()
        return segments
    segments = [seg.strip(" .,-") for seg in remainder.split(",")]
    return [seg for seg in segments if seg]


def _reorder_country_first_dash(text, index):
    """"<Country full name> - City" (and "<noise> - Country - City", e.g.
    "APAC - Australia - Sydney") -- country-first, the opposite order of
    this module's comma convention. Distinct from R8 (a 2-letter ISO-code
    prefix): this is a full country name, arbitrary length, so it can only
    be recognized via a real dataset lookup, not a fixed-width regex.

    Scans left to right for the first dash-segment that's a genuine country
    alias (not merely 2 characters, so it can't misfire on "Hub - UT - Salt
    Lake City" the way a naive first-segment check might) and, once found,
    treats everything from there on as a reversed breadcrumb -- silently
    dropping any leading noise segment ("APAC", "Retail", "Hub") that isn't
    itself a country. Must run before R9's dash-collapsing
    _strip_remote_markers, on the same not-yet-collapsed text R8 uses (see
    plan Key Technical Decisions), or the " - " delimiter this depends on
    is destroyed before it's ever seen. Returns None (try the normal
    pipeline instead) when no segment is a country -- e.g. "Retail - New
    York" or "Remote - CA" have no country-named segment at all, so this
    never fires on them.
    """
    if " - " not in text:
        return None
    parts = [p.strip(" .,-") for p in text.split(" - ")]
    parts = [p for p in parts if p]
    for i, part in enumerate(parts):
        # 2-letter codes are R8's dedicated domain (country_by_prefix_code,
        # which excludes codes colliding with a US state postal abbreviation
        # -- e.g. "GA" = Gabon vs. Georgia). country_by_alias has no such
        # exclusion, so matching a 2-letter part here would resolve "GA -
        # Atlanta" to Gabon instead of correctly deferring to R8 (which
        # already ran and correctly left it unresolved).
        if len(part) > 2 and part in index.country_by_alias:
            reordered = parts[i:]
            reordered.reverse()
            return reordered
    return None


def _resolve_bare(token, index, *, scope_country=None, strict=False, allow_bare_abbrev=False):
    if token in _GENERIC_AREA_DESCRIPTORS:
        return dict(_UNRESOLVED)
    if token in index.ambiguous_bare_tokens:
        return dict(_UNRESOLVED)
    country = index.country_by_alias.get(token)
    if country:
        if scope_country and country != scope_country:
            # A real country hint (R8's prefix) is present but disagrees
            # with the resolved country -- resolving to the mismatched
            # country would silently discard the hint the poster gave us.
            return dict(_UNRESOLVED)
        return {"city": None, "region": None, "country": country, "resolved": True}
    region = index.region_full_by_alias.get(token)
    if region:
        code, country = region
        if scope_country and country != scope_country:
            return dict(_UNRESOLVED)
        return {"city": None, "region": code, "country": country, "resolved": True}
    if allow_bare_abbrev:
        region_matches = index.region_abbrev_by_alias.get(token)
        if region_matches:
            # Bare abbrev code with no head/city at all (e.g. "MI" left
            # after "Hybrid" is stripped from "Hybrid - MI" by
            # _strip_location_noise_words) -- same same-abbrev-collision
            # population tiebreak _resolve_segments' tail lookup already
            # uses (e.g. "CA" California vs. Luxembourg's Capellen
            # district). Gated on allow_bare_abbrev, set only when noise-
            # word stripping actually removed something (see
            # normalize_location): a token the user typed bare, with no
            # noise-word context discarded, must NOT gain this -- e.g. bare
            # "GA" alone must stay unresolved (it's also Gabon's ISO code;
            # R8's dedicated prefix lookup already excludes exactly this
            # class of collision, and this fallback must defer to that same
            # caution, not quietly reopen it for every US state whose
            # 2-letter code happens to double as a country's). Deliberately
            # region_abbrev_by_alias, NOT region_any_by_alias -- the latter
            # also carries comma_context_full_aliases (e.g. "new york" the
            # demoted state name), which must never win a bare lookup or it
            # would silently undo the region-vs-city demotion that lets the
            # city win there (see _GeoIndex).
            code, country = max(
                region_matches, key=lambda m: index.country_population.get(m[1]) or 0
            )
            return {"city": None, "region": code, "country": country, "resolved": True}
    matches = index.city_by_alias.get(token)
    if matches:
        candidates = matches
        if scope_country:
            scoped = [m for m in matches if m["country"] == scope_country]
            if not scoped:
                # A real country hint (R8's prefix, or a resolved tail
                # segment) is present, but no candidate for this token
                # exists there -- resolving to some other country's
                # namesake would silently discard the hint the poster gave
                # us. Stay unresolved rather than guess.
                return dict(_UNRESOLVED)
            candidates = scoped
        # Same-type collision (e.g. multiple cities named "Springfield"):
        # resolve via population then feature-code tier rather than staying
        # unresolved -- the one city type with a reliable secondary signal.
        # Cross-type collisions never reach here; they're caught by the
        # ambiguous_bare_tokens check above (see geodata_generation.py).
        m = _best_city_candidate(candidates)
        return {"city": m["name"], "region": m["region"], "country": m["country"], "resolved": True}
    return dict(_UNRESOLVED)


def _resolve_segments(
    segments, index, *, scope_country=None, strict_city=False, allow_bare_abbrev=False
):
    if not segments:
        return dict(_UNRESOLVED)
    if len(segments) == 1:
        result = _resolve_bare(
            segments[0],
            index,
            scope_country=scope_country,
            strict=strict_city,
            allow_bare_abbrev=allow_bare_abbrev,
        )
        if result["resolved"]:
            return result
        match = _TRAILING_BARE_CODE_RE.match(segments[0])
        if match:
            return _resolve_segments(
                [match.group(1), match.group(2)],
                index,
                scope_country=scope_country,
                strict_city=strict_city,
            )
        return result

    *head, tail = segments
    # A trailing US ZIP code on the tail segment (e.g. "New York, NY
    # 10065") is common in full-address-shaped location strings but was
    # never anticipated by R6's original "last comma segment is the
    # country/region" design -- "ny 10065" matches neither a country nor a
    # region alias verbatim, so the whole string fell through to
    # unresolved despite carrying a perfectly good regional signal. Only
    # strips when it leaves a non-empty remainder, so a segment that's
    # nothing but a ZIP code doesn't collapse to an empty tail.
    zip_stripped_tail = _TRAILING_ZIP_RE.sub("", tail)
    if zip_stripped_tail:
        tail = zip_stripped_tail
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
        if not matches and "|" in candidate:
            # A literal "|" can survive into the comma-tail head segment
            # when the tail alone already resolves without the top-level
            # "|" fallback in normalize_location ever getting a chance to
            # run (e.g. "GA | Atlanta, GA" -- the tail "ga" resolves to
            # Georgia, US on its own, so the whole string is already
            # resolved=True before that fallback's "not resolved" gate is
            # checked). Try each pipe-separated piece of the head as its
            # own city candidate, first genuine match wins, rather than
            # silently losing the city because the raw, unsplit head never
            # matches any alias verbatim.
            for piece in candidate.split("|"):
                piece = piece.strip(" .,-")
                if not piece:
                    continue
                piece_matches = index.city_by_alias.get(piece)
                if piece_matches:
                    matches = piece_matches
                    break
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


_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def normalize_location(raw):
    """Resolve a free-text location string into a structured dict.

    Returns ``{"city": str|None, "region": str|None, "country": str|None,
    "resolved": bool}``. Never raises on ``None``, empty, or malformed input
    — mirrors ``apps/jobs/ingestion/normalizers._derive_is_remote``'s
    never-raise contract, since ingestion and profile-save both call this
    unconditionally on user- or scraper-supplied text.
    """
    result = _normalize_location_once(raw)
    if result["resolved"] or not isinstance(raw, str):
        return result

    # Fallback only, never a first attempt, and tried in this exact order --
    # each stage can only ever rescue an otherwise-unresolved row, never
    # override a resolution the plain whole-string attempt above already
    # found (confirmed necessary by a real regression: some job postings
    # list several "|"-separated offices with a non-place leading blurb,
    # e.g. "Remote-Friendly (Travel Required) | San Francisco, CA" -- the
    # whole string already resolves via the comma-tail machinery reading
    # straight through the leading noise to "CA", so unconditionally
    # splitting on "|" and resolving only the first segment discarded that
    # and lost the row entirely).
    if "|" in raw:
        # Real production gap: some ATSes list several candidate offices
        # separated by "|" (e.g. "San Jose | San Francisco") with no single
        # segment being noise -- try each in order, first segment carrying
        # real place info wins. A segment that merely resolves to
        # _NO_PLACE_INFO (pure noise, e.g. "Remote") must not win the race
        # over a later segment that resolves to an actual place --
        # confirmed as a real regression: "Remote | San Francisco HQ" was
        # silently discarding "San Francisco HQ" because _NO_PLACE_INFO's
        # resolved=True let a noise-only leading segment win by being tried
        # first. A no-place-info segment is remembered and returned only if
        # no later segment carries real place info, so "Remote | Hybrid"
        # still correctly resolves as no-place-info rather than unresolved.
        no_place_info_result = None
        for candidate in raw.split("|"):
            candidate = candidate.strip()
            if not candidate:
                continue
            candidate_result = _normalize_location_once(candidate)
            if not candidate_result["resolved"]:
                continue
            if candidate_result == _NO_PLACE_INFO:
                no_place_info_result = no_place_info_result or candidate_result
                continue
            return candidate_result
        if no_place_info_result is not None:
            return no_place_info_result

    # A trailing parenthetical is sometimes pure noise ("Brasov (30
    # Hermann)", "Bangkok (Central World Office)") and sometimes a genuine
    # disambiguating hint ("Georgia (US)") -- stripping it unconditionally
    # up front could turn a resolvable string into an ambiguous one.
    without_paren = _TRAILING_PAREN_RE.sub("", raw)
    if without_paren != raw:
        paren_result = _normalize_location_once(without_paren)
        if paren_result["resolved"]:
            return paren_result

    return result


def _normalize_location_once(raw):
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
    suffix_stripped = without_suffix != first_segment

    # The dataset is only needed to validate an actual prefix match or to
    # resolve a real place segment -- a bare remote/hybrid string (a large
    # fraction of real job postings) never reaches either, so it shouldn't
    # have to pay for a dataset load at all.
    index = None
    prefix_country = None
    prefix_match = _TWO_LETTER_PREFIX_RE.match(without_suffix)
    if prefix_match:
        index = _try_load_index()
        if index is None:
            return dict(_UNRESOLVED)
        code, remainder_after_prefix = prefix_match.groups()
        prefix_country = index.country_by_prefix_code.get(code)
        without_prefix = remainder_after_prefix.strip() if prefix_country else without_suffix
    else:
        without_prefix = without_suffix

    if " - " in without_prefix:
        if index is None:
            index = _try_load_index()
            if index is None:
                return dict(_UNRESOLVED)
        country_first_segments = _reorder_country_first_dash(without_prefix, index)
        if country_first_segments is not None:
            return _resolve_segments(
                country_first_segments, index, scope_country=None, strict_city=suffix_stripped
            )

    remainder = _strip_remote_markers(without_prefix)
    without_noise = _strip_location_noise_words(remainder)
    # Whether a noise word ("Hybrid", "Office", "HQ", ...) was actually
    # discarded -- gates _resolve_bare's bare-abbrev-region fallback (see
    # its docstring): a bare 2-letter code the user typed directly (e.g.
    # "GA") must stay unresolved, but the same code left over after
    # discarding a noise word ("Hybrid - MI" -> "MI") is safe to resolve,
    # since the noise word was never a competing place-name interpretation
    # to guard against.
    noise_stripped = without_noise != remainder
    remainder = without_noise
    if not remainder:
        # R9: nothing left after remote-marker/noise-word stripping means
        # the input was remote/hybrid/office-noise with no place
        # information -- a defined "resolved, no location" state, not a
        # coverage gap to flag. If R8 already identified a prefix country
        # (e.g. "US - Remote"), preserve it rather than discarding an
        # already-resolved signal.
        if prefix_country:
            return {**_NO_PLACE_INFO, "country": prefix_country}
        return dict(_NO_PLACE_INFO)

    if index is None:
        index = _try_load_index()
        if index is None:
            return dict(_UNRESOLVED)

    segments = _split_segments(remainder)
    # scope_country only applies to a bare (single-segment) remainder -- a
    # multi-segment remainder already carries its own explicit country
    # signal in its tail (see _resolve_segments), which is more reliable
    # than R8's prefix hint and shouldn't be overridden by it.
    return _resolve_segments(
        segments,
        index,
        scope_country=prefix_country,
        strict_city=suffix_stripped,
        allow_bare_abbrev=noise_stripped,
    )
