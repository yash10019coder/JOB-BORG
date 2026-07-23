"""Transforms GeoNames' raw export files into the versioned YAML shape
``apps/locations/engine.py``'s ``_GeoIndex`` consumes.

Kept separate from the management command so it's unit-testable without
Django's command-invocation machinery, and so the same functions can be
exercised against small fixture text (as tests do) or the real multi-MB
GeoNames downloads (as the command does).

Ambiguity handling (see docs/plans/2026-07-23-001-feat-geonames-location-coverage-plan.md
Key Technical Decisions): a bare alias that collides across more than one
distinct real-world entity is never added to a single-valued lookup dict
(``country_by_alias``, ``region_full_by_alias``, ``region_any_by_alias``) --
those dicts have no room for more than one candidate per alias and would
otherwise silently let the last-built entry win. Colliding aliases are
excluded from those dicts entirely and recorded in ``ambiguous_bare_tokens``
instead. Same-type *city* collisions are the sole exception: ``city_by_alias``
is list-valued by design, so multiple same-type city candidates are kept and
resolved at lookup time by ``apps/locations/engine.py``'s feature-code/
population tiebreak -- cities are the one type with a reliable secondary
disambiguation signal (GeoNames' ``feature code`` and ``population`` columns);
countries and regions have no comparable signal in the source files.
"""
import csv
import io
from collections import defaultdict

# Cities at or above this population are included -- keeps the checked-in
# dataset file small while covering the volume of real job postings (see
# origin brainstorm's problem-frame data). Matches GeoNames' own
# `cities15000` export naming.
DEFAULT_MIN_POPULATION = 15000

# v1.yaml's original display names and alias lists for the 5 already-curated
# countries, preserved exactly so previously-resolved strings keep resolving
# to the same values (Success Criteria: "No regression in existing resolved
# locations"). Every other country falls back to its ISO alpha-2 code as the
# display name -- stable, always available, and consistent with how
# admin1CodesASCII.txt joins ("{ISO2}.{admin1code}").
COUNTRY_NAME_OVERRIDES = {
    "US": ("US", ["us", "usa", "united states", "united states of america", "u.s.", "u.s.a."]),
    "GB": ("UK", ["uk", "united kingdom", "great britain", "u.k."]),
    "DE": ("Germany", ["germany", "deutschland"]),
    "IN": ("India", ["india"]),
    "CA": ("Canada", ["canada"]),
}

# Multi-script/historical/airport-code-looking `alternatenames` entries are a
# known GeoNames data-quality issue (see plan's Deferred to Follow-Up Work).
# This is a light, mechanical filter, not an exhaustive cleanup -- drop
# all-caps 3-letter tokens that look like airport/IATA codes; everything
# else is accepted as-is for this iteration.
def _looks_like_airport_code(raw_alias):
    return len(raw_alias) == 3 and raw_alias.isalpha() and raw_alias.isupper()


def _clean_alias(raw):
    alias = raw.strip().lower()
    return alias if alias else None


def parse_countries_file(text):
    """Parse countryInfo.txt -> list of
    {"iso": str, "iso3": str, "name": str, "population": int}.

    Commented header lines (starting with '#') are skipped, matching
    GeoNames' own documented format.
    """
    rows = []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        if len(row) < 8:
            continue
        iso, iso3, _iso_numeric, _fips, name = row[:5]
        if not iso:
            continue
        try:
            population = int(row[7]) if row[7] else 0
        except ValueError:
            population = 0
        rows.append(
            {"iso": iso.strip(), "iso3": iso3.strip(), "name": name.strip(), "population": population}
        )
    return rows


def parse_admin1_file(text):
    """Parse admin1CodesASCII.txt -> {"{ISO2}.{admin1code}": name}."""
    result = {}
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for row in reader:
        if not row or len(row) < 2:
            continue
        code, name = row[0].strip(), row[1].strip()
        if code:
            result[code] = name
    return result


def parse_cities_file(text, *, min_population=DEFAULT_MIN_POPULATION):
    """Parse cities15000.txt (or an equivalent export) -> list of raw city dicts.

    Only rows meeting ``min_population`` are kept (the real export is
    already population-filtered at this threshold, but tests may pass
    smaller/unfiltered fixtures).
    """
    rows = []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for row in reader:
        if not row or len(row) < 15:
            continue
        try:
            population = int(row[14]) if row[14] else 0
        except ValueError:
            population = 0
        if population < min_population:
            continue
        rows.append(
            {
                "name": row[1].strip(),
                "asciiname": row[2].strip(),
                "alternatenames": row[3].strip(),
                "feature_code": row[7].strip(),
                "country_code": row[8].strip(),
                "admin1_code": row[10].strip(),
                "population": population,
            }
        )
    return rows


def _country_display(iso):
    override = COUNTRY_NAME_OVERRIDES.get(iso)
    return override[0] if override else iso


def _build_countries(country_rows):
    """Returns (countries_list, country_by_alias_candidates).

    ``country_by_alias_candidates`` maps alias -> set of ISO codes that
    claimed it, used by the caller to detect same-type collisions before
    committing any alias to the final resolvable dict.
    """
    countries_list = []
    candidates = defaultdict(set)

    for row in country_rows:
        iso = row["iso"]
        display = _country_display(iso)
        override = COUNTRY_NAME_OVERRIDES.get(iso)
        aliases = set(override[1]) if override else set()
        for raw in (iso, row["iso3"], row["name"]):
            alias = _clean_alias(raw)
            if alias:
                aliases.add(alias)

        countries_list.append(
            {
                "name": display,
                "aliases": sorted(aliases),
                "population": row["population"],
                "_iso": iso,
            }
        )
        for alias in aliases:
            candidates[alias].add(iso)

    return countries_list, candidates


def _build_regions(admin1_map):
    """Returns (regions_list, full_alias_candidates, abbrev_alias_candidates).

    Candidates map alias -> set of (country_iso, region_code) pairs, used
    to detect same-type collisions (e.g. "Central" recurring as an admin1
    name across multiple countries) before committing an alias.
    """
    regions_list = []
    full_candidates = defaultdict(set)
    abbrev_candidates = defaultdict(set)

    for code, name in admin1_map.items():
        if "." not in code:
            continue
        country_iso, region_code = code.split(".", 1)
        country_display = _country_display(country_iso)
        key = (country_iso, region_code)

        full_alias = _clean_alias(name)
        abbrev_alias = _clean_alias(region_code) if region_code.isalpha() else None

        regions_list.append(
            {
                "name": name,
                "code": region_code,
                "country": country_display,
                "_country_iso": country_iso,
                "_full_alias": full_alias,
                "_abbrev_alias": abbrev_alias,
            }
        )
        if full_alias:
            full_candidates[full_alias].add(key)
        if abbrev_alias:
            abbrev_candidates[abbrev_alias].add(key)

    return regions_list, full_candidates, abbrev_candidates


def _build_cities(city_rows, admin1_map, *, country_alias_vocabulary=frozenset()):
    """``country_alias_vocabulary`` is the set of already-established country
    aliases. GeoNames' ``alternatenames`` column is a known-messy, uncurated
    dump (see plan Context & Research / External References) -- a low-value
    alternate transliteration for one obscure town can coincidentally match
    a real country's name (confirmed on real data: the Serbian town
    "Inđija" lists "India" among its ~30 alternatenames, which would
    otherwise mark "india" cross-type ambiguous and break resolution for
    the entire country). A city's *primary* name/asciiname is never
    filtered this way -- only entries sourced from the noisier
    alternatenames column are held to the stricter bar.
    """
    cities_list = []
    for row in city_rows:
        country_iso = row["country_code"]
        admin1_key = f"{country_iso}.{row['admin1_code']}" if row["admin1_code"] else None
        region_name = admin1_map.get(admin1_key) if admin1_key else None
        region_code = row["admin1_code"] if region_name else None

        aliases = set()
        for raw in (row["name"], row["asciiname"]):
            alias = _clean_alias(raw)
            if alias:
                aliases.add(alias)
        for raw in row["alternatenames"].split(","):
            raw = raw.strip()
            if not raw or _looks_like_airport_code(raw):
                continue
            alias = _clean_alias(raw)
            if alias and alias not in country_alias_vocabulary:
                aliases.add(alias)

        cities_list.append(
            {
                "name": row["name"],
                "region": region_code,
                "country": _country_display(country_iso),
                "population": row["population"],
                "feature_code": row["feature_code"],
                "aliases": sorted(aliases),
            }
        )
    return cities_list


def build_geodata(city_rows, admin1_map, country_rows, *, min_population=DEFAULT_MIN_POPULATION):
    """Assemble the full v2.yaml-shaped dataset dict from parsed GeoNames rows.

    ``city_rows`` should already be filtered to ``min_population`` by
    ``parse_cities_file`` -- ``min_population`` is accepted here only so
    callers passing pre-parsed rows can re-assert the threshold.
    """
    city_rows = [r for r in city_rows if r["population"] >= min_population]

    countries_list, country_candidates = _build_countries(country_rows)
    regions_list, full_candidates, abbrev_candidates = _build_regions(admin1_map)
    cities_list = _build_cities(
        city_rows, admin1_map, country_alias_vocabulary=frozenset(country_candidates)
    )

    ambiguous = set()
    region_full_aliases_to_drop = set()

    # Cross-type: a bare alias resolving to more than one of {country, region, city}.
    city_alias_index = defaultdict(list)
    for city in cities_list:
        for alias in city["aliases"]:
            city_alias_index[alias].append(city)

    for alias in set(country_candidates) | set(full_candidates) | set(city_alias_index):
        hits_country = alias in country_candidates
        hits_region = alias in full_candidates
        hits_city = alias in city_alias_index
        types_hit = sum((hits_country, hits_region, hits_city))
        if types_hit <= 1:
            continue

        if hits_region and hits_city and not hits_country:
            # Region-vs-city, same country, no country involved (e.g. "New
            # York" the state vs. New York City; "Washington" the state vs.
            # Washington, D.C.) -- v1.yaml's own curation deliberately
            # dropped the region's claim so the city (the overwhelmingly
            # more common real-world meaning for a job-board bare location)
            # wins, rather than failing closed. Confirmed as high-impact on
            # real production data: bare "New York" and "Washington" are
            # both extremely common job-posting location strings, and
            # blanket-failing them closed regressed thousands of rows
            # during implementation spot-checks.
            region_full_aliases_to_drop.add(alias)
        elif hits_country and hits_city and not hits_region:
            # Country-vs-city, no region involved (e.g. city-states like
            # "Singapore", which are both a country and their own city
            # entry in GeoNames). _resolve_bare already checks country
            # before city, so simply not marking this ambiguous lets the
            # existing precedence resolve it -- no exclusion needed.
            pass
        else:
            # Country-vs-region (e.g. "Georgia"), or all three types at
            # once: no comparable "which one is overwhelmingly more common"
            # precedent exists, and the origin brainstorm's own success
            # criteria requires the country/region homograph case to stay
            # unresolved -- fail closed.
            ambiguous.add(alias)

    # Same-type: a bare country alias claimed by more than one distinct ISO code.
    for alias, isos in country_candidates.items():
        if len(isos) > 1:
            ambiguous.add(alias)

    # Same-type: a bare region full-alias claimed by more than one distinct
    # (country, region_code) pair (e.g. "Central" reused across countries).
    for alias, pairs in full_candidates.items():
        if len(pairs) > 1:
            ambiguous.add(alias)

    # Same-type abbrev collisions (e.g. "CA" = California's postal code
    # *and* Luxembourg's Capellen district's admin1 code) are deliberately
    # NOT excluded here, unlike full-alias/country/city collisions. Real
    # data shows this class of collision is common (e.g. many 2-letter
    # admin1 codes are reused across small countries) and one-sided in
    # practice: dropping "CA" entirely to be safe against the Luxembourg
    # case would break the extremely common, high-value "City, ST" pattern
    # for real US states. `apps/locations/engine.py`'s `_GeoIndex` keeps
    # `region_any_by_alias` list-valued for exactly this reason and
    # resolves same-abbrev collisions with a country-population tiebreak
    # at lookup time (mirroring the same-type city tiebreak), rather than
    # excluding the alias at generation time.

    # Cross-type: an ISO country code that's ALSO some region's abbrev code
    # (e.g. "GA" = Gabon's ISO alpha-2 *and* Georgia, US's postal
    # abbreviation -- confirmed on real GeoNames data: 45 such collisions).
    # `_resolve_segments`' tail lookup checks `country_by_alias` before
    # `region_any_by_alias`, so an unresolved collision here doesn't fail
    # closed -- it confidently resolves the wrong thing (e.g. "Atlanta, GA"
    # -> country=Gabon instead of country=US/region=GA). The country alias
    # is the one dropped, not the region abbrev: abbrev aliases are the
    # well-established "City, ST" pattern this dataset exists to serve,
    # while a bare 2-letter country code is one of several aliases that
    # country still has (its ISO3 and full name survive untouched).
    country_abbrev_collisions = set(country_candidates) & set(abbrev_candidates)

    # Build final resolvable dicts, excluding ambiguous aliases entirely.
    for country in countries_list:
        country["aliases"] = [
            a for a in country["aliases"] if a not in ambiguous and a not in country_abbrev_collisions
        ]
    for region in regions_list:
        if region["_full_alias"] in ambiguous:
            region["_full_alias"] = None
        elif region["_full_alias"] in region_full_aliases_to_drop:
            # Region-vs-city same-name collision (e.g. "Washington" the
            # state vs. Washington, D.C.): the alias must stop resolving
            # BARE (so the city wins there, per the fix above), but must
            # keep working in comma-context ("Seattle, Washington") --
            # that pattern has no collision at all, since a comma-qualified
            # tail is never confused with a bare lookup. Demoting to
            # `comma_context_full_alias` (populates region_any_by_alias /
            # region_scoped_by_country_alias only, not region_full_by_alias)
            # instead of dropping it outright preserves that extremely
            # common pattern (confirmed on real data: dropping it outright
            # broke "Seattle, Washington" and 250+ similar rows).
            region["_comma_context_full_alias"] = region["_full_alias"]
            region["_full_alias"] = None

    countries_yaml = [
        {"name": c["name"], "aliases": c["aliases"], "population": c["population"]}
        for c in countries_list
        if c["aliases"]
    ]
    regions_yaml = [
        {
            "name": r["name"],
            "code": r["code"],
            "country": r["country"],
            "full_aliases": [r["_full_alias"]] if r["_full_alias"] else [],
            "comma_context_full_aliases": (
                [r["_comma_context_full_alias"]] if r.get("_comma_context_full_alias") else []
            ),
            "abbrev_aliases": [r["_abbrev_alias"]] if r["_abbrev_alias"] else [],
        }
        for r in regions_list
    ]
    cities_yaml = [
        {
            "name": c["name"],
            "region": c["region"],
            "country": c["country"],
            "population": c["population"],
            "feature_code": c["feature_code"],
            "aliases": c["aliases"],
        }
        for c in cities_list
    ]

    return {
        "version": "v2",
        "countries": countries_yaml,
        "regions": regions_yaml,
        "cities": cities_yaml,
        "ambiguous_bare_tokens": sorted(ambiguous),
    }


HEADER_TEMPLATE = """\
# JobBorg location alias/hierarchy dataset v2.
#
# Machine-generated from GeoNames (https://www.geonames.org/) data, licensed
# CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Derived from
# cities15000.txt (population >= {min_population}), admin1CodesASCII.txt, and
# countryInfo.txt, downloaded {download_date} from
# https://download.geonames.org/export/dump/. See CREDITS.md for the
# repo-level attribution. Regenerate via `manage.py generate_geodata`.
#
# Ambiguous bare aliases (colliding across country/region/city types, or
# across more than one distinct country/region -- e.g. an admin1 name like
# "Central" recurring in multiple countries) are excluded from the
# country/region alias dicts entirely and listed in `ambiguous_bare_tokens`
# instead, so a bare lookup can never silently pick one meaning over another.
# Same-type CITY collisions are the one exception -- kept resolvable, and
# disambiguated at lookup time in apps/locations/engine.py by feature-code
# tier then population (see plan Key Technical Decisions).
#
# Bump `version` (and apps/locations/engine.py's CURRENT_LOCATION_ALIAS_VERSION)
# whenever this file is re-curated, so the sweep task (apps/locations/tasks.py)
# knows to re-normalize already-processed rows.
"""


def render_yaml(data, *, download_date, min_population=DEFAULT_MIN_POPULATION):
    """Render the dataset dict as YAML text with the provenance header."""
    import yaml

    header = HEADER_TEMPLATE.format(download_date=download_date, min_population=min_population)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return header + "\n" + body
