---
date: 2026-07-23
topic: geonames-location-coverage
---

# GeoNames-Backed Worldwide Location Coverage

## Summary

Replace the hand-curated `apps/locations/geodata/v1.yaml` dataset with a GeoNames-derived worldwide dataset (cities with population ≥ 15,000, first-level administrative regions, and countries), regenerated offline and checked in as a new `v2` version through the existing `normalize_location()` engine. Ship alongside three targeted string-format fixes found in real production data. No libpostal, no new database table — the existing versioned-static-file, in-memory-lookup architecture is reused as-is.

---

## Problem Frame

`apps/locations/geodata/v1.yaml` is hand-curated, and by its own header comment "US coverage is deliberately detailed... non-US coverage starts at country level only." A query against production data confirms the resulting gap is large and getting worse, not just theoretical: of 82,326 `Job` rows, only 30,532 (37%) have `location_resolved=True` — down from the 44.5% cited when issue #7 was filed, as the job dataset has grown faster than the hand-curated dataset.

Pulling the top-40 unresolved `location` strings by frequency shows the gap has two independent causes, not one:

1. **US coverage is itself incomplete.** Only 6 of 50 states (NY, CA, TX, GA, IL, WA) are curated. High-volume strings like `Washington, DC` (610 jobs), `Arlington, VA` (309), `Boston, MA` (275), `Miami, FL` (179), `Baltimore, MD` (178), `Denver, CO` (141), `Las Vegas, NV` (140), and `Raleigh, NC` (123) all fail to resolve today purely because their state isn't in the table — nothing to do with international coverage.
2. **Non-US cities have no curation at all**, only country-level aliases. `São Paulo` (436), `Singapore` (435), `Seoul, South Korea` (396), `Madrid` (272), `Tokyo, Japan` (271), `Paris` (256), `Bangkok, Thailand` (182), `Hong Kong` (171), `Barcelona` (131), and `Amsterdam` (130) are all common, unambiguous, well-formed place names that fail purely for lack of data.

A third, smaller class of gaps is pure string-format noise, unrelated to dataset size: LinkedIn-style `"<City> Area"` suffixes (`Bangalore Area`, `Toulouse Area`, `Montreal Area`), country-code prefixes (`SG - Singapore`, `UK - London`, `US - San Francisco`), and bare remote/hybrid markers (`Remote` — 1,247 jobs, `Hybrid`, `World Wide - Remote`) that the engine's remote-marker stripping doesn't fully absorb today.

Growing the hand-curated YAML file entry-by-entry to reach worldwide coverage isn't practical — GeoNames (free, CC-BY-licensed, 12M+ place dataset with population data and administrative hierarchy) already solves this and was identified as the deferred follow-up in issue #8, gated on exactly this kind of coverage-gap evidence.

Related: `apps/locations/engine.py`, `apps/locations/geodata/v1.yaml`, `apps/jobs/admin.py` (`location_resolved` filter), GitHub issues #7, #8, #10.

---

## Requirements

**Dataset replacement**
- R1. `apps/locations/geodata/v1.yaml`'s hand-curated country/region/city lists are replaced by a dataset derived from GeoNames' city (population ≥ 15,000), admin1 region, and country data, covering the whole world at the same level of structural depth v1.yaml gave the US.
- R2. The new dataset is generated offline (not fetched live at request time or import time) and checked into the repo as a new versioned file, following the same `lru_cache`-loaded-once pattern `apps/locations/engine.py` already uses — no new runtime dependency, no database table, no live network call from `normalize_location()`.
- R3. `apps.locations.engine.CURRENT_LOCATION_ALIAS_VERSION` is bumped to reflect the new dataset, so the existing `sweep_stale_locations` mechanism automatically re-normalizes every `Job` and `Profile` row without any new code path.
- R4. The comma-segment resolution logic already in `apps/locations/engine.py` (country/region/city segment matching, remote-marker stripping, multi-location first-segment handling) is reused as-is against the new dataset — this requirement is about dataset breadth, not a rewrite of the matching algorithm.

**Ambiguity handling at worldwide scale**
- R5. When a bare, unqualified place name matches more than one entry of the *same type* in the new dataset (e.g. multiple cities named "Springfield") and no comma-context disambiguates it, resolution picks the highest-population match rather than leaving the location unresolved. This fallback applies only to same-type collisions (city vs. city); it does not apply to a bare token that collides *across* country/region/city types (e.g. "Georgia" the country vs. Georgia the US state) — those remain unresolved, preserving `apps/locations/geodata/v1.yaml`'s existing `ambiguous_bare_tokens` fail-closed behavior, which this dataset swap does not change.
- R6. Comma-qualified strings that already resolve unambiguously via existing region/country context (e.g. `"Springfield, MA"`) are unaffected by R5 — the ambiguity fallback only applies to genuinely context-free bare tokens.

**String-format fixes**
- R7. A trailing `" Area"` suffix on a location string (e.g. `"Bangalore Area"`) is stripped before place matching, so the underlying city can still resolve.
- R8. A leading two-letter country-code prefix followed by a separator and place name (e.g. `"SG - Singapore"`, `"UK - London"`) is recognized and the place-name portion is matched normally. GeoNames keys some countries under ISO codes that differ from common informal abbreviations (e.g. the United Kingdom is `GB` in GeoNames' country data, not `UK`) — prefix recognition needs a small hand-maintained alias mapping for these cases rather than assuming GeoNames' own codes cover every commonly-used prefix.
- R9. Bare remote/hybrid indicator strings that carry no place information (`"Remote"`, `"Hybrid"`, `"World Wide - Remote"`) are handled by the existing remote-marker/`is_remote` path rather than being scored as an unresolved location — this may mean treating them as an expected non-location case rather than a resolution failure.

**Licensing**
- R10. GeoNames data usage in the app includes CC-BY attribution somewhere reasonably discoverable (e.g. a README or about/credits location) — this repo has not previously depended on CC-BY-licensed data and has no existing attribution convention to reuse.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3.** Given the new dataset lands and `CURRENT_LOCATION_ALIAS_VERSION` is bumped, when the periodic `sweep_stale_locations` task next runs, previously-unresolved `Job` rows with common non-US cities (e.g. `location="São Paulo"`) or missing US states (e.g. `location="Boston, MA"`) resolve to structured city/region/country without any ingestion or manual re-trigger.
- AE2. **Covers R5, R6.** Given a `Job` with `location="Springfield"` (no other context) and GeoNames data containing multiple Springfields, when normalization runs, the job resolves to the highest-population Springfield; given `location="Springfield, MA"`, the job resolves to the Massachusetts Springfield specifically, regardless of population ranking.
- AE3. **Covers R7, R8.** Given `location="Bangalore Area"` or `location="SG - Singapore"`, when normalization runs, both resolve to their underlying city (Bangalore, India and Singapore respectively) rather than staying unresolved.
- AE4. **Covers R9.** Given `location="Remote"` with no other location text, when normalization runs, the job is not counted as a location-resolution failure (whether by resolving to a defined remote/no-location state or being excluded from the unresolved-tracking metric is left to planning).

---

## Success Criteria

- The `location_resolved=True` rate across existing `Job` rows rises substantially above the current 37% after the sweep completes, driven by both the US state-coverage gap and non-US city coverage closing.
- The top unresolved-location patterns observed in this brainstorm (Washington DC, Arlington VA, São Paulo, Singapore, Seoul, Madrid, Tokyo, etc.) all resolve correctly after this ships, without manual per-entry curation.
- No regression in existing resolved locations — strings that resolved correctly under v1.yaml (e.g. `"Atlanta, GA"`, ambiguous-country/state homographs like bare `"Georgia"`) continue to resolve the same way, or fail closed the same way, under the new dataset.
- A downstream implementer can regenerate the GeoNames-derived dataset file from a documented, repeatable process (not a one-off hand edit), so future GeoNames updates don't require re-deriving the generation logic from scratch.

---

## Scope Boundaries

- libpostal / full free-text address parsing is out of scope — the observed gaps are simple place names and fixable string-format noise, not messy multi-line postal addresses. Revisit only if messy-address-shaped gaps actually surface after this ships (tracked as the remaining open half of issue #8).
- A database-backed, live-queried GeoNames import (full `allCountries` dataset, 12M+ places including towns under 15,000 population) is out of scope — the bundled offline-regenerated file, filtered to cities ≥ 15,000 population, is the chosen shape. Towns below that threshold remain unresolved for now.
- Radius/"near me" search (issue #9) is unaffected and remains separately deferred.
- Any change to `apps/matching/scoring.py`'s hierarchy-matching logic is out of scope — this work only changes what data `normalize_location()` matches against, not how scoring consumes structured location fields.

---

## Key Decisions

- GeoNames over libpostal for this pass: the actual unresolved-location data (São Paulo, Singapore, Seoul, Madrid, etc.) is dominated by simple, well-formed place names and three fixable string-format patterns — not garbled free-text addresses, which is libpostal's specialty. GeoNames alone (a comprehensive worldwide place database) addresses the observed gap without the deployment cost of a compiled C library and multi-gigabyte parser data files in the web/worker Docker images.
- Bundled, offline-regenerated file over a live-queried database table: preserves the existing `apps/locations/engine.py` architecture (pure, in-memory, `lru_cache`-loaded-once, no DB round-trip per `normalize_location()` call) instead of introducing a new import pipeline and query path. Trade-off: coverage stops at the population threshold chosen for the bundled file (≥ 15,000), and updates require regenerating and redeploying the file rather than a live sync.
- Highest-population match for unqualified ambiguous names: at GeoNames' city-count scale, hand-listing every ambiguous bare token (as v1.yaml did for the small number of US collisions) doesn't scale. Defaulting to the largest/most-likely-meant place trades a small amount of precision for materially more coverage, consistent with how most people actually mean an unqualified city name.

---

## Dependencies / Assumptions

- Assumes GeoNames' `cities15000` (or equivalent population-filtered city export), `admin1CodesASCII`, and `countryInfo` files remain available and usable under their CC-BY license for this purpose (publicly documented as free/open; not separately re-verified in this brainstorm beyond the license terms being CC-BY).
- Assumes the existing `apps/locations/engine.py` `_GeoIndex` lookup structure (alias-keyed dicts for country/region/city) can represent GeoNames-scale data (tens of thousands of city entries) without a redesign — not verified against real GeoNames export volume/shape in this brainstorm; flagged for planning to confirm.
- Assumes `sweep_stale_locations`'s existing batching (`LOCATION_BACKFILL_BATCH_SIZE`) is sufficient to re-normalize all ~82k `Job` rows plus every `Profile` in a reasonable time window after the version bump — not separately sized in this brainstorm.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] Exact GeoNames source files to use (`cities15000` vs. a different population cutoff), and the concrete offline generation script/process (language, location in the repo, how it maps GeoNames' column format into `apps/locations/engine.py`'s existing `_GeoIndex` shape).
- [Affects R7, R8, R9][Technical] Whether the three string-format fixes are implemented as pre-processing steps in `apps/locations/engine.py` (before dataset lookup) or as changes to the dataset/alias structure itself.
- [Affects R9][Technical] Whether bare remote/hybrid strings with no place information should be represented as `resolved=True` with all fields `None` (a defined "no location, and that's fine" state) vs. simply excluded from `location_resolved=False` reporting/metrics while keeping today's `resolved=False` value — affects both the engine's return contract and the admin `location_resolved` filter's meaning.
- [Affects R5][Needs research] Whether GeoNames' population figures are reliable/current enough across all countries to trust for disambiguation, or whether some regions need a different tiebreaker (e.g. capital-city preference) — flagged for planning to spot-check against a sample of the actual ambiguous-name collisions in the generated dataset.
- [Affects R10][Needs research] Confirm current CC-BY attribution requirements for GeoNames specifically (attribution text/link format) and identify where in the app it should live.
