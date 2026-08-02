---
date: 2026-07-30
type: fix
origin: docs/brainstorms/2026-07-30-location-resolution-gap-fixes-requirements.md
status: completed
---

# Fix: Close the Location-Resolution Gap (Prefix Collisions, Canada Provinces, Country Synonyms)

## Summary

Fix three confirmed root causes behind the ~17% `location_resolved=False` rate: R8's country-code-prefix matching drops 75 countries' ISO codes on incidental region-abbreviation collision (fixed via a dedicated, always-available ISO2 lookup that still excludes the ~21 codes that collide with US state postal abbreviations); Canadian provinces have no abbreviation aliases at all because GeoNames' Canadian admin1 codes are numeric, not alphabetic; and only 5 countries have hand-curated common-name synonyms, leaving Korea/Netherlands/Taiwan/Hong Kong/Czech Republic (and possibly others found during implementation) unresolvable by their everyday English names. All three land in `apps/locations/geodata_generation.py` and are picked up by a version-bumped regeneration of the dataset (`v2` → `v3`), reusing the existing sweep-based re-normalization mechanism from PR #27 with no new code path.

---

## Problem Frame

(See origin: `docs/brainstorms/2026-07-30-location-resolution-gap-fixes-requirements.md` for the full data-grounded investigation — 75-country collision enumeration, 557-row Canada impact count, and per-country synonym-gap counts.) In short: `normalize_location()`'s existing architecture (versioned YAML dataset + in-memory lookup dicts, `apps/locations/engine.py`) is sound; these are data-generation and pre-processing bugs within that architecture, not a design change.

---

## Requirements

- R1–R3 (origin). Dedicated ISO2 lookup for R8's country-code-prefix matching, excluding the ~21 US-state-colliding codes.
- R4 (origin). Canadian province/territory postal abbreviations registered as region aliases.
- R5 (origin). Country common-name synonym curation expanded to at least Korea, Netherlands, Taiwan, Hong Kong, Czech Republic.

---

## Key Technical Decisions

- **The dedicated ISO2 lookup is generated data, not a hand-maintained constant.** It lives in `geodata_generation.py`'s `build_geodata()` output as a new top-level field, mechanically derived at generation time by intersecting all country ISO codes against a small, genuinely-fixed `US_STATE_POSTAL_CODES` constant (the 50 states + DC — this list itself never changes, unlike "which countries happen to collide," which depends on GeoNames' current admin1 data and should be recomputed on every regeneration). This resolves the origin document's deferred question in favor of the generated-data approach — it keeps the exclusion set correct automatically as GeoNames data updates, consistent with how every other alias set in this file is already derived rather than hand-listed.
- **Canada's province abbreviations are a small hardcoded reference table, not derived from GeoNames.** GeoNames' Canadian admin1 codes are numeric (`CA.01`–`CA.13`) and carry no postal-abbreviation information to derive from — Canada Post's 13 province/territory codes are stable, official, public data with no realistic drift, so a hand-maintained mapping (numeric code → postal abbreviation) is added, in the same spirit as `COUNTRY_NAME_OVERRIDES`'s existing hand-curation pattern for the 5 special-cased countries.
- **Version bump to `v3`, not an in-place `v2.yaml` rewrite.** `apps/locations/engine.py`'s own documented invariant is that a logic-only change without a version bump leaves already-processed rows silently stale, because `sweep_stale_locations` keys off `location_alias_version` vs. `CURRENT_LOCATION_ALIAS_VERSION`. This plan bumps to `v3`, writes a new `apps/locations/geodata/v3.yaml`, and keeps `v2.yaml` checked in as historical record — the same pattern PR #27 already used and validated for the `v1` → `v2` cutover, including the `backfill_locations --dry-run` safety net.
- **New country synonyms follow the existing `COUNTRY_NAME_OVERRIDES` shape exactly** (display name + alias list per ISO code) rather than a new data structure — this is additive curation, not a new mechanism.

---

## Scope Boundaries

(Carried forward from origin — see that document's Scope Boundaries for the full list and rationale.) Reversed `"Country - City"` order, non-comma delimiters, additional suffix-noise stripping, `"N Locations"` placeholders, doubled prefixes, a systemic 190-country synonym overhaul, and below-minimum-population cities are all explicitly out of scope for this plan.

### Deferred to Follow-Up Work

- Broader country-synonym curation beyond the 5 confirmed countries, if implementation-time spot-checking of the full 3,690-distinct-unresolved-string sample (not just the top-40) surfaces additional clear, high-frequency gaps — origin document leaves this to implementer judgment (see origin Outstanding Questions), but any such additions should be called out explicitly in the eventual PR description rather than silently expanding scope mid-implementation.

---

## Implementation Units

### U1. Add `US_STATE_POSTAL_CODES` constant and dedicated ISO2-prefix lookup to `build_geodata()`

**Goal:** Generate a new, always-populated country-code → country-display-name lookup for R8's prefix matching, independent of the ambiguity-filtered `country_by_alias` dict, correctly excluding codes that collide with US state postal abbreviations.

**Requirements:** R1, R2, R3 (origin)

**Dependencies:** None

**Files:**
- Modify: `apps/locations/geodata_generation.py` (add `US_STATE_POSTAL_CODES` constant near `COUNTRY_NAME_OVERRIDES`; add logic in `build_geodata()` to compute the new lookup from `countries_list` — the same `_build_countries()` output already available before the ambiguity filter runs — excluding any ISO code present in `US_STATE_POSTAL_CODES`; add the new field to the returned dataset dict, e.g. `country_iso2_prefixes`)
- Test: `apps/locations/tests/test_geodata_generation.py`

**Approach:** The new lookup is built directly from each country's ISO alpha-2 code and display name (via `_country_display()`), computed *before* the existing collision-drop logic runs on `country_by_alias` — it's a parallel, independent structure, not a filtered view of the existing one. Only the US-state-collision exclusion applies to it; the region-abbrev-collision exclusion that currently drops `SG` etc. from `country_by_alias` does not apply here, since ISO codes are globally unique by construction and the R8 prefix-matching context has no ambiguity to guard against beyond the US-state case.

**Patterns to follow:** `_build_countries()`'s existing `candidates` dict-building shape; `COUNTRY_NAME_OVERRIDES`'s existing module-level constant style for `US_STATE_POSTAL_CODES`.

**Test scenarios:**
- Happy path: `build_geodata()`'s output includes an entry for Singapore's `SG` code mapping to its display name, using a small fixture with a Switzerland "St. Gallen" (`CH.SG`) region row alongside a Singapore country row — proving the SG collision no longer suppresses the prefix-lookup entry (this exact collision is the confirmed real-data root cause).
- Edge case: a country whose ISO code matches `US_STATE_POSTAL_CODES` (e.g. a fixture using `GA` for a fictitious or real country) is excluded from the new lookup even though it would otherwise qualify — using a minimal fixture, not the full 21-code enumeration.
- Edge case: `US_STATE_POSTAL_CODES` itself never collides with the country lookup's own real entries incorrectly — the exclusion only suppresses countries whose ISO code happens to also be a US state code, not all countries.
- Regression: countries in `COUNTRY_NAME_OVERRIDES` (US, UK, Germany, India, Canada) still appear correctly in the new lookup using their display names, not raw ISO codes.

**Verification:** `build_geodata()`'s output contains the new field with correct entries for the SG/St. Gallen collision case and correctly excludes a US-state-colliding fixture country.

---

### U2. Wire the dedicated ISO2 lookup into R8's prefix matching in `engine.py`

**Goal:** Make `normalize_location()`'s R8 prefix-recognition step use the new lookup instead of `country_by_alias`, fixing `"SG - Singapore"` and equivalent strings while leaving `"GA - Atlanta"` unresolved.

**Requirements:** R1, R2, R3 (origin)

**Dependencies:** U1

**Files:**
- Modify: `apps/locations/engine.py` (`_GeoIndex.__init__` gains a new `country_by_prefix_code` dict populated from `data.get("country_iso2_prefixes")`; the R8 block in `normalize_location()` looks up `index.country_by_prefix_code.get(code)` instead of `index.country_by_alias.get(code)`)
- Test: `apps/locations/tests/test_engine.py`

**Approach:** This is a narrow, single-line substitution at the point where `prefix_country = index.country_by_alias.get(code)` currently runs — everything else about R8's control flow (regex match, remainder stripping, `scope_country` threading into `_resolve_segments`) is unchanged. `_GeoIndex` needs a small fixture-loadable test dataset (or an in-memory dict built directly, following `test_engine.py`'s existing fixture pattern) that includes the new field, since `_load_index()` reads the real `v2.yaml`/`v3.yaml` file.

**Patterns to follow:** `test_engine.py`'s existing test structure for R8 (`test_prefix_country_scopes_the_remainder_instead_of_being_discarded`, `test_prefix_country_with_no_matching_remainder_stays_unresolved`, added in the prior code-review round) — this unit extends that same test class.

**Test scenarios:**
- Happy path. Covers AE1. `normalize_location("SG - Singapore")` resolves to `country="SG"`, using a test dataset where Singapore's `SG` is present in the new prefix lookup but absent from `country_by_alias` (mirroring the real collision).
- Edge case. Covers AE1. `normalize_location("GA - Atlanta")` stays unresolved, using a test dataset where `GA` is deliberately absent from the prefix lookup (simulating the US-state exclusion) — proves the exclusion is honored at the engine level, not just the generation level.
- Regression: existing R8 tests (`SG - Singapore` doesn't appear currently, but `UK - London`, `NZ - Cambridge`, `MX - Berlin` do) continue to pass unchanged, proving the substitution doesn't alter behavior for codes present in both lookups.

**Verification:** `python manage.py test apps.locations` passes, including the new SG/GA test cases and all existing R8 regression tests.

---

### U3. Add Canadian province/territory postal abbreviations

**Goal:** `"City, <Canadian province code>"` strings (e.g. `"Toronto, ON"`, `"Calgary, AB"`) resolve correctly.

**Requirements:** R4 (origin)

**Dependencies:** None (independent of U1/U2)

**Files:**
- Modify: `apps/locations/geodata_generation.py` (`_build_regions()` — add a small hardcoded `CANADA_PROVINCE_POSTAL_CODES` mapping of GeoNames' numeric Canadian admin1 codes to the standard 2-letter postal abbreviation; when building each region's `_abbrev_alias`, fall back to this mapping when `region_code.isalpha()` is false and the country is Canada)
- Test: `apps/locations/tests/test_geodata_generation.py`

**Approach:** The existing `abbrev_alias = _clean_alias(region_code) if region_code.isalpha() else None` line is the exact point that silently produces `None` for every Canadian province (numeric codes). Add a lookup against the new Canada-specific table as a fallback when the direct alpha-check fails and the row's country is Canada, leaving the behavior for every other country unchanged. This flows through the existing `abbrev_candidates`/same-abbrev-collision machinery unchanged (Canada's abbreviations don't currently collide with anything already registered, but the existing list-valued `region_any_by_alias` handles it safely either way if that changes in a future GeoNames update).

**Patterns to follow:** `COUNTRY_NAME_OVERRIDES`'s hand-curated-constant style; the existing `_build_regions()` per-row abbrev-alias derivation logic.

**Test scenarios:**
- Happy path. Covers AE2. A fixture Ontario admin1 row (`CA.08 -> Ontario`) produces an `_abbrev_alias` of `"on"`, using a minimal admin1 fixture — proving the fallback fires for Canada's numeric codes.
- Edge case: a non-Canadian country with a numeric admin1 code (if any exist in practice) is unaffected — the fallback only applies when the country is Canada.
- Regression: existing alphabetic-code countries (e.g. US states, already alpha) are unaffected by the new fallback branch, since it only triggers when `region_code.isalpha()` is false.

**Verification:** `normalize_location("Toronto, ON")`, `normalize_location("Calgary, AB")`, `normalize_location("Vancouver, BC")`, and `normalize_location("Montreal, QC")` all resolve to their correct city/province/`country="Canada"` against the regenerated dataset (verified in U5's real-data check, not just the unit fixture test here).

---

### U4. Expand `COUNTRY_NAME_OVERRIDES` with confirmed missing synonyms

**Goal:** `"Seoul, Korea"`, `"Amsterdam, Netherlands"`, `"Taipei, Taiwan"`, and equivalent strings for Hong Kong and Czech Republic resolve using their common English names.

**Requirements:** R5 (origin)

**Dependencies:** None (independent of U1–U3)

**Files:**
- Modify: `apps/locations/geodata_generation.py` (`COUNTRY_NAME_OVERRIDES` dict — add entries for `KR` (South Korea: `["korea", "south korea", "republic of korea"]`), `NL` (Netherlands: `["netherlands", "the netherlands", "holland"]`), `TW` (Taiwan: `["taiwan"]`), `HK` (Hong Kong: `["hong kong"]`), `CZ` (Czech Republic: `["czech republic", "czechia"]`) — exact alias lists to be finalized during implementation by checking each ISO code's current GeoNames-derived aliases don't already cover the term, avoiding redundant entries)
- Test: `apps/locations/tests/test_geodata_generation.py`

**Approach:** Purely additive to the existing dict — no logic change. `_build_countries()` already merges `COUNTRY_NAME_OVERRIDES` aliases with GeoNames' own `(iso, iso3, name)` fields, so this is data-only.

**Patterns to follow:** The existing 5-entry `COUNTRY_NAME_OVERRIDES` dict shape exactly.

**Test scenarios:**
- Happy path: `build_geodata()`'s country list includes `"korea"`, `"netherlands"`, `"taiwan"`, `"hong kong"`, and `"czech republic"` as aliases for their respective ISO codes, using minimal country-row fixtures for KR/NL/TW/HK/CZ.
- Regression: the 5 existing overridden countries (US/UK/Germany/India/Canada) are unaffected by the new entries.

**Verification:** `python manage.py test apps.locations.tests.test_geodata_generation` passes with the new alias assertions.

---

### U5. Regenerate the dataset as `v3`, bump the version, and verify against real production data

**Goal:** Ship all three fixes together as a single version cutover, reusing the existing dry-run safety net before the sweep task re-normalizes live rows.

**Requirements:** R1–R5 (origin)

**Dependencies:** U1, U2, U3, U4

**Files:**
- Create: `apps/locations/geodata/v3.yaml` (generated output, not hand-written)
- Modify: `apps/locations/engine.py` (`CURRENT_LOCATION_ALIAS_VERSION = "v3"`)
- Test: none new — this unit is verification/wiring, covered by U1–U4's tests plus the existing `test_engine.py` regression suite and `backfill_locations --dry-run`

**Approach:** Run `manage.py generate_geodata` (the existing management command from PR #27) to produce `v3.yaml` from the same GeoNames source files used for `v2.yaml`, now passing through the U1/U3/U4 logic changes. Bump `CURRENT_LOCATION_ALIAS_VERSION`. Before deploying, run `manage.py backfill_locations --dry-run --json` (the safety-net tooling added in PR #27's code-review round) against a production-data snapshot or the live database to confirm: (a) the specific strings named in the origin document's problem frame now resolve correctly, (b) no previously-resolved row's value changes unexpectedly (a same-type ambiguity tiebreak silently picking a different candidate), consistent with the existing `v1`→`v2` cutover's verification approach.

**Patterns to follow:** PR #27's `v1` → `v2` cutover sequence exactly (`docs/plans/2026-07-23-001-feat-geonames-location-coverage-plan.md`'s version-cutover unit) — same dry-run-then-bump-then-sweep flow, no new mechanism needed.

**Test scenarios:**
- Integration. Covers AE1, AE2, AE3. Regenerate `v3.yaml` from real GeoNames source data and confirm via a live `normalize_location()` check (not just unit fixtures) that `"SG - Singapore"`, `"Toronto, ON"`, `"Calgary, AB"`, `"Seoul, Korea"`, `"Amsterdam, Netherlands"`, and `"Taipei, Taiwan"` all resolve, while `"GA - Atlanta"` stays unresolved.
- Regression: `python manage.py test apps.locations apps.matching` passes in full against the regenerated dataset — no existing test in `test_engine.py`'s `BareAliasNoRegressionTests` or the R7/R8/R9 string-format test classes regresses.
- Dry-run check: `manage.py backfill_locations --dry-run --json` output, run against representative production data, shows the expected new resolutions with no unexpected value changes among already-resolved rows.

**Verification:** `v3.yaml` exists and loads correctly; `CURRENT_LOCATION_ALIAS_VERSION == "v3"`; full test suite passes; dry-run output confirms the three confirmed root-cause strings now resolve and no regressions appear among previously-resolved rows.

---

## System-Wide Impact

- `apps/jobs/admin.py`'s `location_resolved=False` filter's population shrinks further, same intended effect as PR #27's cutover — no code change to that filter needed.
- `apps/matching/scoring.py` is an unaffected downstream consumer (same boundary as the origin `v1`→`v2` plan) — it sees improved coverage as input, no logic change.
- The existing `sweep_stale_locations` Celery task re-normalizes all `Job` and `Profile` rows automatically once the version bumps, with no new code path — same mechanism validated in the prior cutover.

---

## Dependencies / Assumptions

(Carried forward from origin, plus:) Assumes the GeoNames source files used to generate `v2.yaml` (or freshly re-downloaded equivalents) are available for regenerating `v3.yaml` via the existing `generate_geodata` management command — not separately verified as still cached/available in this planning pass.
