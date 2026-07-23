---
title: GeoNames-Derived Worldwide Location Coverage
type: feat
status: completed
date: 2026-07-23
origin: docs/brainstorms/2026-07-23-geonames-location-coverage-requirements.md
deepened: 2026-07-23
---

# GeoNames-Derived Worldwide Location Coverage

## Summary

Swap `apps/locations/geodata/v1.yaml`'s hand-curated location dataset for one machine-generated from GeoNames' `cities15000`, `admin1CodesASCII`, and `countryInfo` exports, checked in as `v2.yaml` and consumed through the existing `normalize_location()` engine with no structural changes to its call sites. A new management command performs the offline generation (parse GeoNames' tab-separated exports, auto-detect cross-type name collisions, write the versioned YAML). `apps/locations/engine.py` gains a same-type-only ambiguity fallback (feature-code tier, then population, as the tiebreak) and an ordered string pre-processing pass for three real production string-format gaps. Bumping `CURRENT_LOCATION_ALIAS_VERSION` to `v2` is the only trigger needed for the existing `sweep_stale_locations` task to re-normalize every `Job` and `Profile` row — no new sweep mechanism.

---

## Problem Frame

See origin document for the full data-backed problem statement (37% of 82,326 `Job` rows resolve today; the gap splits into incomplete US state coverage, zero non-US city coverage, and fixable string-format noise). Planning-specific framing: this repo has no existing precedent for generating a checked-in dataset from an external source — `apps/classification`'s versioned-YAML pattern (which `apps/locations` already mirrors) is hand-authored, not machine-generated — so the generation step is new ground, while the consumption side (`engine.py`, the version-stamp sweep) is a proven, reusable pattern.

---

## Requirements

- R1. `apps/locations/geodata/v1.yaml`'s hand-curated country/region/city lists are replaced by a dataset derived from GeoNames' city (population ≥ 15,000), admin1 region, and country data, covering the whole world at the same structural depth v1.yaml gave the US.
- R2. The new dataset is generated offline and checked into the repo as a new versioned file, following the existing `lru_cache`-loaded-once pattern — no new runtime dependency, no database table, no live network call from `normalize_location()`.
- R3. `CURRENT_LOCATION_ALIAS_VERSION` is bumped to `v2`, so the existing `sweep_stale_locations` mechanism automatically re-normalizes every `Job` and `Profile` row.
- R4. The comma-segment resolution logic already in `engine.py` is reused as-is against the new dataset.
- R5. A bare, unqualified place name matching more than one *same-type* entry (city vs. city) and not disambiguated by comma-context resolves to the best candidate rather than staying unresolved; cross-type collisions (country vs. region vs. city) stay unresolved via `ambiguous_bare_tokens`.
- R6. Comma-qualified strings that already resolve unambiguously via region/country context are unaffected by R5.
- R7. A trailing `" Area"` suffix is stripped before place matching.
- R8. A leading two-letter country-code prefix + separator (e.g. `"SG - Singapore"`) is recognized and stripped before place matching.
- R9. Bare remote/hybrid strings with no place information are handled as a defined non-location case, not scored as a resolution failure.
- R10. GeoNames CC-BY attribution is discoverable somewhere in the repo.

**Origin acceptance examples:** AE1 (covers R1, R2, R3), AE2 (covers R5, R6), AE3 (covers R7, R8), AE4 (covers R9)

---

## Scope Boundaries

- libpostal, a DB-backed live GeoNames import, and any change to `apps/matching/scoring.py` remain out of scope (carried from origin).
- The full `alternateNamesV2.zip` alias table (with `isPreferredName`/`isHistoric`/language tags) is out of scope — this plan uses `cities15000.txt`'s embedded `alternatenames` column only, which is lower-precision but requires no second large download or new parsing path. Revisit if alias quality proves to be a real problem after this ships.
- A combined "remote-marker + country-code-prefix" string in a single input (e.g. a hypothetical `"Remote - UK - London"`) is not specifically handled — not observed in the actual unresolved-location data pulled during the brainstorm, so left as an accepted gap rather than added pipeline complexity.

### Deferred to Follow-Up Work

- Filtering `alternatenames`' messy multi-script/historical/airport-code-looking entries: deferred to implementation as a spot-check decision (see Open Questions).
- Non-ISO country-code aliases beyond `UK`→`GB`: deferred to implementation, to be discovered from real generated-dataset spot-checks rather than exhaustively enumerated up front.

---

## Context & Research

### Relevant Code and Patterns

- `apps/locations/engine.py` — `_GeoIndex` (`country_by_alias`, `region_full_by_alias`, `region_any_by_alias`, `region_scoped_by_country_alias`, `city_by_alias: dict[str, list[dict]]`, `ambiguous_bare_tokens: set`), `_load_index()` (`@lru_cache`, keyed on `CURRENT_LOCATION_ALIAS_VERSION`), `normalize_location()`'s pipeline (`_clean` → `_first_multi_location_segment` → `_strip_remote_markers` → `_split_segments` → `_resolve_bare`/`_resolve_segments`). `country_by_alias` and `region_full_by_alias`/`region_any_by_alias` are plain single-valued dicts (`self.region_full_by_alias[alias] = (code, country)`) — safe under v1.yaml's US-only-region, 5-country dataset, but not safe once regions/countries are generated worldwide (see Key Technical Decisions: this plan requires generation-time same-type collision detection for countries and regions, not just cities, since these dicts have no room for more than one candidate per alias and would silently last-write-wins otherwise).
- `apps/locations/geodata/v1.yaml` — the header-comment convention (dense provenance/rationale prose) this plan extends for `v2.yaml`'s GeoNames attribution.
- `apps/classification/engine.py` / `apps/classification/rulesets/v1.yaml` — the versioned-static-data sibling pattern `apps/locations` already mirrors; hand-authored, not machine-generated, so it does not cover the generation-script half of this plan.
- `apps/locations/services.py` (`backfill_jobs`, `backfill_profiles` — version-guarded conditional updates, race-safe) and `apps/locations/tasks.py` (`sweep_stale_locations`) — reused unchanged; R3 requires zero new code here, only the version-constant bump.
- `apps/jobs/management/commands/add_job_source.py`, `apps/locations/management/commands/backfill_locations.py` — the repo's only two management commands, both thin `BaseCommand` wrappers delegating to importable service functions; the pattern the new generation command follows.
- `apps/jobs/ingestion/normalizers.py` (four ATS adapters) and `apps/web/forms.py` (`ProfileForm.save()`) — existing `normalize_location()`/`normalize_target_locations()` call sites, unchanged by this plan.
- `apps/jobs/migrations/0007_backfill_job_locations.py` — precedent for resolving models via `apps.get_model(...)` in data migrations; not needed here since no schema changes are required, only a version-constant bump.

### Institutional Learnings

- `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md` — origin story of `apps/locations/engine.py`'s disambiguation model. Two invariants must survive this plan unchanged: (1) an unrecognized tail segment must never fall back to an unconstrained head-only city match ("confidently wrong is worse than unresolved" — already enforced by `_resolve_segments`, must not regress under broader v2 coverage); (2) substring-fallback in `apps/matching/scoring.py` fires only when the *job's* location is itself unresolved, never merely because a profile target is unresolved — this plan does not touch that logic but the improved coverage shifts more jobs into the "resolved" bucket, which is the intended effect, not a risk.

### External References

- GeoNames export format (verified live): `cities15000.txt` (19 tab-separated columns — `geonameid`, `name`, `asciiname`, `alternatenames`, `latitude`, `longitude`, `feature class`, `feature code`, `country code`, `cc2`, `admin1 code`, `admin2/3/4 code`, `population`, `elevation`, `dem`, `timezone`, `modification date`); `admin1CodesASCII.txt` (`{ISO2}.{admin1code}`, name, name-ascii, geonameid); `countryInfo.txt` (`ISO`, `ISO3`, ..., `Country`, `Capital`, ...). Source: https://download.geonames.org/export/dump/readme.txt, live file inspection.
- GeoNames uses ISO 3166-1 alpha-2 codes exclusively — the United Kingdom is `GB`; `"UK"` is explicitly reserved/non-ISO per `countryInfo.txt`'s own header comment. Other historical/edge codes to treat defensively: `CS`, `AN` (obsolete), `XK` (Kosovo, GeoNames-specific non-ISO).
- GeoNames `feature code` (`PPLC` = capital, `PPLA`/`PPLA2`/... = admin-division seat) is a more stable disambiguation signal than population, which has documented staleness and duplicate-row issues (Ahlers, "Assessment of the Accuracy of GeoNames Gazetteer Data", GIR 2013).
- GeoNames data is CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/); GeoNames' own stated requirement is loose ("give credit... with a link or another reference to GeoNames"), but CC-BY 4.0 also requires indicating modification of the source material.
- Download URLs are stable (`https://download.geonames.org/export/dump/`) but content updates in place with no version pinning — the generation command should record the download date in `v2.yaml`'s header for reproducibility/auditability.

---

## Key Technical Decisions

- **Feature-code tier, then population, as the same-type tiebreak (not population alone):** GeoNames' `feature code` (`PPLC`/`PPLA`/...) is a more stable "is this the primary place" signal than population, which is known to drift and have duplicate-row issues. Resolves the origin document's own deferred question about population reliability using a signal already present in the same source file, at no extra data-fetching cost.
- **Ambiguity auto-detected at generation time, not hand-curated — and generalized beyond cross-type:** the generation command scans the parsed dataset for any bare name that resolves to more than one of {country, region, city} (cross-type) **and also any bare name that collides with itself within country or within region** (e.g. an admin1 name like "Central" or "Western" recurring across multiple countries' `admin1CodesASCII.txt` entries, or two countries sharing an alias) — both classes are added to `ambiguous_bare_tokens` and excluded entirely from `country_by_alias`/`region_full_by_alias`/`region_any_by_alias`, since those dicts have no mechanism to hold more than one candidate per alias (unlike `city_by_alias`, which is already list-valued). **Same-type *city* collisions are the sole exception**, routed to U2's feature-code/population tiebreak instead of `ambiguous_bare_tokens`, since cities are the one type where GeoNames provides a reliable secondary disambiguation signal (`feature_code`, `population`) — countries and regions have no comparable signal in `admin1CodesASCII.txt`, so their same-type collisions fail closed to unresolved rather than guessing. This both fixes v1.yaml's "doesn't scale past a handful of manually-noticed collisions" problem and structurally guarantees R5's same-type-only boundary: `_resolve_bare`'s existing `ambiguous_bare_tokens` check runs before country/region/city branches, so any ambiguous token (cross-type, or same-type country/region) is always caught before it could reach the city-only tiebreak logic. No new precedence logic or list-valued dicts are needed in `_resolve_bare` itself — ambiguous aliases are simply absent from the resolvable dicts by construction.
- **String-format fixes run suffix-strip → prefix-strip → existing remote-marker-strip, on the pre-dash-collapse segment:** R7 and R8 are both anchored (end/start) and don't interact with each other, but both must run before the existing `_strip_remote_markers`, whose `[\s\-–—]+` collapse-to-space regex would otherwise destroy the `" - "` delimiter R8 depends on to recognize a prefix at all. Verified against the real observed patterns `"SG - Singapore Area"` (suffix-strip then prefix-strip both apply cleanly, in either order since they don't overlap) and `"US - Boston, MA"` (prefix-strip must run before comma-segment splitting, since `_split_segments` only splits on commas and would otherwise treat `"US - Boston"` as one unsplit token).
- **R9 resolves to `{"city": None, "region": None, "country": None, "resolved": True}` for a no-place-info remote/hybrid string, not `resolved=False`:** distinguishes "there is genuinely no place to curate" from "the dataset hasn't caught up yet" — the latter is what `location_resolved=False` exists to surface for curation prioritization (per `apps/jobs/admin.py`'s filter), and a bare `"Remote"` string gives a curator nothing actionable to add. `"world wide"` is added to `REMOTE_MARKERS` so `"World Wide - Remote"` (named as real evidence in the origin document) is covered by the same fix, not left as a partial case.
- **Dataset generation as a Django management command, not a standalone script:** matches this repo's only two existing command precedents (`add_job_source`, `backfill_locations`), both thin `BaseCommand` wrappers delegating to importable logic — keeps the generation logic independently testable and consistent with repo convention, even though no prior command has generated an offline data file before.
- **`cities15000.txt`'s embedded `alternatenames` column, not the separate `alternateNamesV2.zip`:** avoids a second large download and a new parsing path for language/preferred-name tags this plan doesn't need; accepted as lower-precision (see Scope Boundaries).

---

## Open Questions

### Resolved During Planning

- Population-only vs. more reliable tiebreak (origin's deferred question): resolved to feature-code tier first, population second.
- Hand-curated vs. automatic cross-type ambiguity detection: resolved to automatic, at generation time.
- R7/R8/R9 pre-processing vs. dataset-structure change, and composition order: resolved to pre-processing, applied before `_split_segments`, in the order suffix-strip → prefix-strip → existing remote-marker-strip.
- R9's `resolved=True` vs. `resolved=False` for no-place-info strings: resolved to `resolved=True` with all fields `None`.
- R10's attribution placement: resolved to a new root `CREDITS.md` plus a header comment in `v2.yaml` itself.
- Exact GeoNames source files: resolved to `cities15000.txt`, `admin1CodesASCII.txt`, `countryInfo.txt` (embedded `alternatenames` only, not `alternateNamesV2.zip`).

### Deferred to Implementation

- Exact non-ISO country-code alias list for R8 beyond `UK`→`GB`: enumerate from real spot-checks of the generated dataset and any patterns found in the `location_resolved=False` admin queue, rather than guessing exhaustively now.
- Whether `alternatenames`' messy multi-script/historical/airport-code-looking entries need filtering before use as aliases: spot-check a sample during generation-command implementation and decide (e.g., drop all-caps 3-letter tokens that look like airport codes) or accept as-is for this iteration.
- Whether `LOCATION_BACKFILL_BATCH_SIZE`'s current default (500) is sized adequately for a ~82k-row-plus-all-Profiles sweep in a reasonable number of Beat cycles — confirm empirically once the v2 dataset lands, adjust the setting if the sweep is taking unexpectedly long.
- Whether R8's country-code-prefix stripping needs an explicit precedence rule for two-letter codes that coincide with common US state abbreviations (e.g. ISO `IN` = India vs. Indiana, `LA` = Laos vs. Louisiana) — not evidenced as a real ambiguous case in the production data pulled during the brainstorm, so left for a spot-check during U3 implementation rather than designed for now.

---

## Implementation Units

### U1. GeoNames dataset generation command

**Goal:** A new management command downloads (or accepts locally-provided copies of) GeoNames' `cities15000`, `admin1CodesASCII`, and `countryInfo` exports, transforms them into `apps/locations/geodata/v2.yaml` in the shape `_GeoIndex` expects (extended with `population` and `feature_code` on city entries), auto-detects cross-type name collisions into `ambiguous_bare_tokens`, and writes GeoNames CC-BY attribution into both the file header and a new `CREDITS.md`.

**Requirements:** R1, R2, R10

**Dependencies:** None

**Files:**
- Create: `apps/locations/management/commands/generate_geodata.py` (thin `BaseCommand`, delegates to importable logic)
- Create: `apps/locations/geodata_generation.py` (or equivalent importable module — parsing, transformation, ambiguity detection; kept separate from the command so it's unit-testable without Django's command-invocation machinery)
- Create: `apps/locations/geodata/v2.yaml` (the generated, checked-in output — produced by running the command once during implementation, then committed)
- Create: `CREDITS.md` (repo root)
- Create: `apps/locations/tests/test_geodata_generation.py`

**Approach:**
- Fetch (via `requests`, already a dependency) or accept local file paths for the three GeoNames exports; parse each per the documented tab-separated column format.
- Build `countries` (ISO alpha-2 → name, with a small hand-maintained non-ISO alias list layered on top for R8's benefit, e.g. `UK`→`GB`), `regions` (via `admin1CodesASCII.txt`'s `{ISO2}.{admin1code}` join), and `cities` (population ≥ 15,000 filter already applied by using `cities15000.txt`) entries, matching v1.yaml's existing schema shape but with `population` and `feature_code` added to each city entry.
- Auto-detect ambiguity in two passes: (1) cross-type — a bare name resolving to more than one of {country, region, city}; (2) same-type country or region collisions — a bare country name or bare region name recurring for more than one distinct country/region entry (real-world example: admin1 names like "Central"/"Western"/"Eastern" recur across many countries in `admin1CodesASCII.txt`). Both classes route to `ambiguous_bare_tokens` and are excluded from `country_by_alias`/`region_full_by_alias`/`region_any_by_alias` entirely. Same-type *city* collisions are NOT included here — those are intentionally left resolvable with multiple candidates in `city_by_alias` (already list-valued) for U2's tiebreak to handle. This subsumes v1.yaml's hand-picked list (`cambridge`, `portland`, `georgia`) as a special case that should now be produced automatically.
- Write `v2.yaml` with `version: v2` (the file is newly created, so this is set once at generation time, not bumped later) and a header comment documenting: GeoNames CC-BY 4.0 attribution and license link, the download date, and which source files were used — mirroring `v1.yaml`'s existing dense-provenance-comment convention.
- `CREDITS.md` at repo root gives a second, more discoverable attribution surface per R10's "reasonably discoverable" requirement, independent of whether a reader happens to open the data file.

**Patterns to follow:**
- `apps/jobs/management/commands/add_job_source.py`, `apps/locations/management/commands/backfill_locations.py` — thin command, delegate to importable logic.
- `apps/locations/geodata/v1.yaml`'s header comment style.

**Test scenarios:**
- Happy path: given sample rows in GeoNames' documented column format for a city, a region, and a country, the generation logic produces `_GeoIndex`-shaped entries with the correct aliases, `population`, and `feature_code`.
- Happy path: a name appearing as both a country and a city in the sample input is added to `ambiguous_bare_tokens` (and excluded from `country_by_alias`); a name appearing only as a city is not.
- Happy path: two distinct countries sharing an alias, or two distinct regions (in different countries) sharing an alias, are both added to `ambiguous_bare_tokens` and excluded from `country_by_alias`/`region_full_by_alias`/`region_any_by_alias` — confirms same-type collision detection isn't limited to cross-type.
- Happy path: two distinct cities sharing a bare alias are NOT added to `ambiguous_bare_tokens` — both remain in `city_by_alias` as separate list entries for U2's tiebreak to resolve at lookup time.
- Edge case: a city row with a missing/blank `admin1 code` (some countries have none) does not crash generation and produces a city entry with `region=None`.
- Edge case: the UK→GB (and similar) alias layering is present in the generated country lookup even though GeoNames' own `countryInfo.txt` never contains `"UK"`.
- Integration: running the full command against small fixture files (not the real multi-hundred-MB downloads) produces a syntactically valid YAML file loadable by `apps/locations/engine.py::_load_index`.

**Verification:**
- The generated `v2.yaml` loads successfully via `apps.locations.engine._load_index("v2")` with no `LocationDataError`.
- `CREDITS.md` and `v2.yaml`'s header both name GeoNames, the CC-BY 4.0 license, and a link to geonames.org.

---

### U2. Same-type ambiguity fallback and feature-code/population tiebreak

**Goal:** `_resolve_bare` (and `_resolve_segments`' narrowed-candidate path) resolve same-type bare/narrowed collisions to the best candidate by feature-code tier then population, while cross-type collisions continue to resolve via `ambiguous_bare_tokens` (now auto-populated by U1) exactly as before.

**Requirements:** R1, R4, R5, R6

**Dependencies:** U1 (needs `v2.yaml` and its `population`/`feature_code` fields to test against)

**Files:**
- Modify: `apps/locations/engine.py` (`_resolve_bare`, `_resolve_segments`, `_GeoIndex`)
- Modify: `apps/locations/tests/test_engine.py`

**Approach:**
- No change to `_resolve_bare`'s existing check order (`ambiguous_bare_tokens` → country → region → city) — U1's generalized auto-detection (cross-type, plus same-type country/region) means any such ambiguous token is already caught by the first check, before it could reach the city branch. This keeps R5's same-type-only boundary structurally guaranteed rather than re-implemented as new precedence logic.
- In `_resolve_bare`'s city branch only, when more than one candidate matches (`len(matches) > 1`), select the candidate with the highest feature-code tier (`PPLC` > `PPLA`/`PPLA2`/... > plain `PPL`), using population as the tiebreak only within the same tier — rather than the previous behavior of returning unresolved for any count other than exactly 1.
- **Scoped strictly to `_resolve_bare`, matching R5's literal wording ("a bare, unqualified place name").** `_resolve_segments`' narrowed-candidate path (where `len(narrowed) > 1` after country/region narrowing) keeps its existing behavior — a partial match (`city=None`, `region`/`country` populated) — unchanged. Extending the tiebreak there would mean guessing a specific city for a same-name-same-region collision with no test coverage proving the pick is right; the existing partial-match behavior is already a safe, informative answer for that case and R6 doesn't require anything stronger.
- Regression test directly reproducing the origin document's own named example: bare `"Georgia"` (country vs. US state) stays unresolved, unaffected by the new tiebreak logic, because it's cross-type and caught by `ambiguous_bare_tokens` before the city branch ever runs.

**Patterns to follow:**
- `_resolve_segments`' existing "unrecognized tail → stay unresolved, don't guess" invariant (see Institutional Learnings) — the tiebreak only ever picks among *already-matched* same-type candidates; it never invents a match where none of the existing narrowing logic found one.

**Test scenarios:**
- Happy path (Covers AE2): bare `"Springfield"` with multiple same-type city matches resolves to the highest feature-code-tier match (or highest population within the same tier).
- Happy path (Covers AE2): `"Springfield, MA"` (comma-qualified) resolves to the Massachusetts Springfield specifically, regardless of population ranking elsewhere — R6's "unaffected by R5" guarantee.
- Regression (Covers AE2, Success Criteria): bare `"Georgia"` (country vs. US state, cross-type) stays unresolved — not silently resolved to either meaning by the new same-type tiebreak.
- Edge case: a same-type collision where all candidates share the same feature-code tier falls through to population as the tiebreak.
- Edge case: `_resolve_segments`' narrowed-candidate path with more than one remaining match after country/region narrowing keeps its existing partial-match behavior (`city=None`, `region`/`country` populated) unchanged — confirms the tiebreak was not extended beyond `_resolve_bare`.
- Regression: two distinct countries (or two distinct regions in different countries) sharing a bare alias both stay unresolved via `ambiguous_bare_tokens`, confirming same-type country/region collisions fail closed rather than guessing (no feature-code/population signal exists for these types).
- Edge case: an unrecognized tail segment (e.g. `"Austin, Georgia"` where "Georgia" doesn't resolve as a valid region for the implied country) still returns fully unresolved — confirms the existing "no confident partial match" invariant survives this change.

**Verification:**
- `normalize_location` is deterministic (same input always produces the same output) even when multiple same-type candidates exist.
- The full existing `test_engine.py` suite passes against the v2 dataset with no unexpected resolution changes for previously-resolved v1 strings (e.g. `"Atlanta, GA"`).

---

### U3. String-format pre-processing fixes

**Goal:** `normalize_location()` strips a trailing `" Area"` suffix, recognizes and strips a leading country-code prefix, and treats a bare remote/hybrid string with no remaining place text as a defined non-location state — in an order that doesn't let the fixes destroy each other's delimiters.

**Requirements:** R7, R8, R9

**Dependencies:** U2 (touches the same file; sequenced to avoid parallel-edit conflicts)

**Files:**
- Modify: `apps/locations/engine.py` (`normalize_location`, `REMOTE_MARKERS`)
- Modify: `apps/locations/tests/test_engine.py`

**Approach:**
- Add an ordered pre-processing pass to `normalize_location`, applied to the cleaned first segment, before `_strip_remote_markers` runs: (1) strip a trailing `" Area"` suffix (case-insensitive, anchored at the end); (2) recognize and strip a leading two-letter country-code prefix followed by a separator (anchored at the start), using the GeoNames-derived country codes plus the small non-ISO alias list from U1 (e.g. `UK`→`GB`).
- This order is chosen because both fixes are anchored (start/end) and don't interact with each other, but both must run before the existing `_strip_remote_markers`, whose dash-collapsing regex would otherwise destroy the `" - "` delimiter R8 needs to recognize a prefix at all.
- Add `"world wide"` to `REMOTE_MARKERS` so `"World Wide - Remote"` (named as real evidence in the origin document) resolves the same way as bare `"Remote"`.
- Change the empty-after-remote-marker-strip return value from `_UNRESOLVED` to `{"city": None, "region": None, "country": None, "resolved": True}` — R9's defined "no place info, and that's fine" state.
- Explicitly out of scope: a combined remote-marker-plus-country-prefix string in one input (see Scope Boundaries) — not evidenced in real data, left unhandled.

**Patterns to follow:**
- `_strip_remote_markers`'s existing regex style for the new suffix/prefix stripping helpers.

**Test scenarios:**
- Happy path (Covers AE3): `"Bangalore Area"` resolves to Bangalore, India.
- Happy path (Covers AE3): `"SG - Singapore"` resolves to Singapore.
- Happy path (Covers AE4): bare `"Remote"` returns `resolved=True` with all fields `None`, not `resolved=False`.
- Happy path: `"World Wide - Remote"` resolves the same way as bare `"Remote"`.
- Integration (R7 + R8 combined, real-data-shaped): `"SG - Singapore Area"` — suffix-strip and prefix-strip both apply, resolving to Singapore.
- Integration (R8 + existing comma logic): `"US - Boston, MA"` — prefix-strip removes `"US - "` before comma-segment splitting, resolving to Boston, MA, US (once MA is in the v2 dataset via U1/U2).
- Edge case: `"UK - London"` resolves via the non-ISO `UK`→`GB` alias layered on top of GeoNames' own `GB`-only country data.
- Edge case: a string with only a country-code prefix and no place after it (malformed input) stays unresolved rather than raising.

**Verification:**
- `normalize_location` never raises for any of the new pre-processing inputs, including malformed ones.
- The full `test_engine.py` suite (including U2's tests) still passes — the new pre-processing pass doesn't change resolution for strings that don't match any of the three new patterns.

---

### U4. Version cutover and sweep verification

**Goal:** Bump `CURRENT_LOCATION_ALIAS_VERSION` to `v2`, confirming the existing sweep mechanism re-normalizes all `Job` and `Profile` rows with no new code, and document the resulting shift in `location_resolved`'s meaning.

**Requirements:** R3

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `apps/locations/engine.py` (`CURRENT_LOCATION_ALIAS_VERSION = "v2"`, matching v1.yaml's documented convention of bumping the code constant and the checked-in file's `version:` field together — the latter is already set by U1 at generation time, not modified here)
- Modify: `apps/locations/services.py` (new `diff_stale_locations(job_model, profile_model, batch_size=None)` function — computes `normalize_location()`/`normalize_target_locations()` for every stale row without writing, and returns/yields `(pk, old_value, new_value)` for rows where the resolved city/region/country would *change*, not just newly-resolve; reuses the same batched-read pattern as `backfill_jobs`/`backfill_profiles` but performs no `.update()`)
- Modify: `apps/locations/management/commands/backfill_locations.py` (add a `--dry-run` flag that calls `diff_stale_locations` and prints a report instead of calling `backfill_jobs`/`backfill_profiles`)
- Modify: `apps/locations/tests/test_tasks.py`, `apps/locations/tests/test_services.py` (confirm existing assertions still hold; spot-check any resolved-value assertions like `"Austin, TX, US"` → `region="TX"` against the new dataset; add the bare-alias regression scenario below; add `diff_stale_locations` unit tests)

**Approach:**
- This is a one-line functional change (the version constant) — the existing `sweep_stale_locations` task, `backfill_jobs`/`backfill_profiles` service functions, and their race-safety guarantees are reused entirely unchanged, exactly as R3 specifies.
- No new migration is needed: unlike the original field-addition migrations (`0007_backfill_job_locations.py` and the `Profile` equivalents), this is a version-string change picked up by the existing sweep/backfill mechanism, not a schema change.
- Confirm existing `test_tasks.py` sweep tests (which stub staleness via an empty-string version, largely version-agnostic already) pass unmodified; only tests asserting specific resolved *values* need re-verification against the new dataset's content.
- **Before flipping the version constant**, run `backfill_locations --dry-run` (backed by the new `diff_stale_locations` service function) against production data, specifically to catch U2's same-type tiebreak silently picking a different city for a bare alias that v1 already resolved uniquely (e.g. `"London"` resolving to a different London once v2's worldwide same-type collisions exist). This is the one path in this plan where a wrong value can land silently — v1.yaml had at most one candidate per bare alias, so today's resolved bare-city matches have never exercised the tiebreak logic at all.
- `sweep_stale_locations`'s `backfill_jobs`/`backfill_profiles` loop (`apps/locations/services.py`) drains its entire backlog (`while True` until no stale rows remain) in one task invocation, not incrementally across Beat ticks, and no `task_time_limit`/`task_soft_time_limit` is configured for it. The first post-cutover run will attempt all ~82k stale `Job` rows plus every `Profile` row in a single long-running task call. Document this explicitly (see Risks & Dependencies) rather than assuming Beat-cycle throttling that the code doesn't implement.

**Test scenarios:**
- Happy path (Covers AE1): after the version bump, `sweep_stale_locations` re-normalizes a `Job` row with a previously-unresolved common location string (e.g. `"São Paulo"`, `"Boston, MA"`) to `location_resolved=True` with correct structured fields.
- Regression: a sample of v1.yaml's bare, uniquely-resolved city aliases (e.g. `"london"`, `"toronto"`, `"chicago"`, `"munich"`, `"bangalore"`) each resolve under v2 to the *same* city they resolved to under v1 — not merely "still resolved." This is the concrete check for U2's tiebreak silently picking a different same-type candidate for input that previously had no ambiguity to resolve.
- Integration: existing `test_services.py` assertions for specific resolved values (e.g. `"Austin, TX, US"`) still produce the same result under the v2 dataset.
- Test expectation: none beyond the above — the sweep/backfill mechanism itself has no behavioral change, only new data flowing through it.

**Verification:**
- Running the sweep (or the `backfill_locations` management command) against a database with pre-v2 rows converges every row to `location_alias_version == "v2"`.
- `backfill_locations --dry-run`'s report against production data shows zero (or an explicitly reviewed, accepted) set of value-changing resolutions for previously-resolved rows before the version bump ships.
- `apps/jobs/admin.py`'s `location_resolved=False` filter continues to work as a curation-priority signal — its population shrinks to reflect the genuinely-uncurated remainder (excluding U3's now-resolved remote-only jobs), which is the intended effect, not a functional change to the admin code itself.

---

## System-Wide Impact

- **Interaction graph:** `apps/jobs/ingestion/normalizers.py`'s four ATS call sites and `apps/web/forms.py`'s `ProfileForm.save()` are unchanged — they call the same `normalize_location()`/`normalize_target_locations()` functions with the same return shape. `apps/matching/scoring.py` is a downstream consumer of `location_resolved`/`location_city`/etc. and sees improved coverage as an input; its logic is unchanged (per origin Scope Boundaries).
- **Error propagation:** `normalize_location`'s never-raise contract is preserved throughout; no new exception paths introduced.
- **State lifecycle risks:** the version-bump sweep re-normalizes ~82k `Job` rows plus all `Profile` rows via `backfill_jobs`/`backfill_profiles`' internal batch loop, which drains its entire backlog in one task invocation rather than yielding back to Beat between batches — the first post-cutover run is effectively one long-running, unbounded (no `task_time_limit` configured) task call, not a naturally-throttled multi-tick sweep. While it runs, two jobs with textually identical unresolved locations could transiently score differently against the same profile depending on which has been re-normalized so far. The batched-write mechanism itself (conditional updates, idempotent) is unchanged and safe; only the "spread across many Beat cycles" framing needs correcting — it is one big cycle, not several small ones, unless the task is killed mid-run (in which case it resumes safely on the next Beat tick, since progress is durable per-row).
- **API surface parity:** `apps/jobs/admin.py`'s `location_resolved` `list_filter` meaning shifts slightly (R9's remote-only jobs now count as `resolved=True`) — a documentation note, not a code change.
- **Integration coverage:** at least one end-to-end test confirming a real production unresolved string (e.g. `"São Paulo"`) resolves correctly after the full v2 dataset + sweep, not just isolated `engine.py` unit tests.
- **Unchanged invariants:** `apps/matching/scoring.py`'s hierarchy-matching algorithm; `apps/locations`' dependency-free-leaf boundary (no imports of `jobs`/`accounts`/`matching`); `services.py`'s conditional-update race-safety pattern for backfill writes.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `country_by_alias`/`region_full_by_alias`/`region_any_by_alias` are plain single-valued dicts with no room for multiple candidates per alias — worldwide GeoNames data can produce same-type country or region collisions (e.g. recurring admin1 names like "Central"/"Western" across countries) these dicts would otherwise silently last-write-wins on | U1's generation-time ambiguity detection is generalized to catch same-type country/region collisions (not just cross-type), routing them to `ambiguous_bare_tokens` and excluding them from the resolvable dicts entirely — see Key Technical Decisions |
| GeoNames' `alternatenames` column is known to contain messy multi-script, historical, and airport-code-looking entries — **and**, given U1's automatic ambiguity detection, a bad alias that happens to collide with a curated country/region name would newly mark a previously-clean token `ambiguous_bare_tokens` (a regression toward *more* unresolved, not just false-positive matches) | Deferred spot-check decision during U1 implementation (filter obviously-bad entries or accept as-is for this iteration); the dry-run diff added to U4 also surfaces any such newly-unresolved bare tokens before the version bump ships |
| U2's same-type feature-code/population tiebreak silently resolves a previously-unique bare city alias (e.g. `"London"`) to a *different* city than v1 did, since v1's small dataset never had same-type collisions for these to begin with | U4's dry-run diff (`backfill_locations --dry-run`) and a named regression test asserting a sample of v1's bare-resolved aliases resolve to the same city under v2, before the version bump ships |
| `sweep_stale_locations` drains its entire ~82k-row-plus-`Profile` backlog in one unbounded task invocation (no `task_time_limit` configured), not incrementally across Beat cycles | Documented explicitly in System-Wide Impact and U4 rather than assumed self-throttling; the underlying per-row writes are idempotent and resumable if the task is killed mid-run, so a long/interrupted run degrades gracefully rather than corrupting data |
| CC-BY attribution insufficient or overlooked | `CREDITS.md` + `v2.yaml` header comment satisfies GeoNames' stated (loose) requirement; both are created in U1, not left to a later pass |

---

## Documentation / Operational Notes

- `CREDITS.md` (new, repo root) documents the GeoNames CC-BY 4.0 dependency.
- `apps/jobs/admin.py`'s `location_resolved=False` filter's population and meaning shift after this ships (shrinks to the genuinely-uncurated remainder, excludes remote-only jobs) — worth a one-line mention to whoever curates location data via that filter.
- No new environment variables, Celery Beat schedule entries, or deployment changes — reuses `LOCATION_BACKFILL_BATCH_SIZE` and the existing `sweep_stale_locations` Beat entry unchanged.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-07-23-geonames-location-coverage-requirements.md](../brainstorms/2026-07-23-geonames-location-coverage-requirements.md)
- Related code: `apps/locations/engine.py`, `apps/locations/geodata/v1.yaml`, `apps/locations/services.py`, `apps/locations/tasks.py`, `apps/jobs/ingestion/normalizers.py`, `apps/web/forms.py`, `apps/jobs/admin.py`, `apps/classification/engine.py`
- Related issues: #7, #8, #10
- Institutional learning: `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md`
- External docs: https://download.geonames.org/export/dump/readme.txt, https://www.geonames.org/export/, https://creativecommons.org/licenses/by/4.0/
