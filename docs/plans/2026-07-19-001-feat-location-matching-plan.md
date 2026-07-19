---
title: Structured Location Matching
type: feat
status: active
date: 2026-07-19
---

# Structured Location Matching

## Summary

Replace naive lowercase-substring location matching with structured normalization: job locations (at ingestion) and profile target locations (at save time) get parsed into city/region/country fields by a new, self-hosted, curated alias/hierarchy dataset (`apps/locations`, mirroring the existing `apps/classification` versioned-YAML-engine pattern). Scoring compares structured fields with hierarchy-aware matching instead of substrings, falls back to today's substring logic when a location can't be resolved, and fixes a confirmed bug where `ONSITE_ONLY` profiles currently ignore `target_locations` entirely. A version-stamped sweep mechanism handles future alias-table re-curation.

---

## Problem Frame

Users reported "location filters aren't working." Investigation found two compounding issues: (1) `apps/matching/scoring.py`'s `_location_component()` gives `ONSITE_ONLY` profiles full location credit for *any* onsite job, silently ignoring `target_locations`; (2) even where location is checked (the `ANY` branch), it's a raw lowercase-substring comparison, which breaks on abbreviations ("NY" vs. "New York"), produces false positives (e.g. "ny" inside "Albany"), and has no concept of hierarchy (a user targeting "California" won't match a job listed as "San Francisco, CA"). The job source (`apps/jobs/ingestion/board_search.py`'s Greenhouse-companies dataset) is not US-restricted, so this needs to work for locations worldwide, not just the US.

---

## Requirements

- R1. Job locations and profile target locations are normalized into structured city/region/country fields via a curated, self-hosted alias/hierarchy dataset (no paid geocoding API), replacing substring-only matching.
- R2. `_location_component()` no longer ignores `target_locations` for `ONSITE_ONLY` profiles — onsite jobs are compared against the profile's structured target locations the same way the `ANY` branch does, with hierarchy-aware matching. `REMOTE_ONLY` continues to ignore `target_locations` (remote work has no meaningful location).
- R3. Locations the alias table can't resolve fall back to the existing substring-matching behavior and are tagged `resolved=False` for coverage visibility — no dedicated manual-review-queue subsystem in v1.
- R4. Existing `Job.location` and `Profile.target_locations` data are backfilled into the new structured fields via a data migration that is safely interleavable with concurrent ingestion/profile-save traffic (this is the repo's first `RunPython` data migration).
- R5. Alias-table version bumps (future re-curation) have a defined re-normalization trigger — a periodic sweep mirroring `apps/classification`'s `needs_classification`/`ruleset_version` pattern — so stale normalized rows don't silently persist.
- R6. Coverage is global from day one (not US-only): detailed US state/metro/abbreviation data, thinner country-level-only coverage elsewhere. Hierarchy matching treats a profile's unspecified levels (city/region) as "don't care," not "must match."

---

## Scope Boundaries

- Multi-location postings (e.g. "New York, NY or Remote") parse only the first segment in v1; the remainder is not represented, and the string is flagged `resolved=False` if the first segment alone doesn't resolve.
- No hard location gate added to `apps/matching/prefilter.py` — location remains a soft-scored component only, consistent with how salary and tags are treated (never silently dropped, unknown is neutral/fallback rather than exclusionary).
- No paid geocoding/autocomplete API, no radius/"near me" search, no GeoNames/libpostal pipeline — all deferred per the originating brainstorm.
- No dedicated manual-review queue or UI for unresolved locations — visibility is via a Django admin filter (`location_resolved=False`) only.

### Deferred to Follow-Up Work

- GeoNames + libpostal broader coverage pipeline: future iteration, once international/long-tail coverage gaps (visible via the `resolved=False` admin filter this plan adds) prove to matter.
- Radius/"near me" search (PostGIS): future iteration, once it's an actual product requirement.
- Whether an alias-table version bump should proactively enqueue a full rematch sweep for affected profiles, or just fix forward on the next natural rematch trigger: deferred to implementation, defaulting to fix-forward (see Open Questions) to avoid an unbounded rematch storm on every alias-table curation pass.

---

## Context & Research

### Relevant Code and Patterns

- `apps/classification/engine.py` (`load_ruleset`, `lru_cache`, `CURRENT_RULESET_VERSION`) + `apps/classification/rulesets/v1.yaml` — the versioned-static-dataset pattern this plan's alias table follows.
- `apps/classification/tasks.py` (`sweep_unclassified`, `_classify_batch`) + `Job.needs_classification`/`Job.ruleset_version` — the stale-row sweep pattern this plan's alias-version sweep mirrors.
- `apps/jobs/ingestion/normalizers.py` (`_derive_is_remote`, `_REMOTE_MARKERS`) — precedent for deriving a field from the raw location string once, at ingestion, never re-derived downstream.
- `apps/jobs/ingestion/upsert.py` (`_CONTENT_FIELDS`) and `apps/jobs/ingestion/config.py` (`_HASH_FIELDS`) — the fields-copied-onto-row vs. fields-that-trigger-reclassification distinction this plan must respect (structured location joins the former, not the latter).
- `apps/matching/services.py` (`profile_snapshot`, `job_snapshot`, `candidate_profiles_for_job`) — the DB-free snapshot-dict boundary that `apps/matching/scoring.py` and `apps/matching/prefilter.py` sit behind; must stay DB-free.
- `apps/jobs/management/commands/add_job_source.py` — thin `Command` delegating to an importable service function; the pattern for any backfill/reload management command this plan adds.
- `apps/jobs/admin.py` (`DiscoveredBoardAdmin`'s `similar_employer_hint`) — precedent for an admin-surfaced review hint, used here for the `location_resolved=False` filter.
- `apps/matching/tests/factories.py`, `apps/matching/tests/test_scoring.py` — plain-function factories (not factory_boy) and `SimpleTestCase` for pure/DB-free logic; the test style to extend.

### Institutional Learnings

- None — `docs/solutions/` does not exist in this repo yet. This feature is a good `/ce-compound` candidate afterward (alias-table-vs-substring-matching rationale, the version-stamped-sweep pattern, and the `ONSITE_ONLY` scoring bug are all reusable lessons).

### External References

- Gathered during the originating brainstorm: LinkedIn's `jobPostingGeoLocations` API (structured geo entities over free text), GeoNames (free, CC-BY, 12M+ place dataset with aliases/hierarchy), libpostal (free-text address parsing, no API). These informed the "structured entity, not string" direction but are not directly integrated in this plan — see Deferred to Follow-Up Work.

---

## Key Technical Decisions

- **New `apps/locations` app, not a `apps/matching` submodule**: `apps/matching` already depends on `apps/jobs` and `apps/accounts` (imports `Job`, `Profile`); having `apps/jobs/ingestion` and `apps/web/forms.py` import a normalizer from `apps/matching` would run that dependency backwards. `apps/locations` is a leaf app with no models and no dependency on `jobs`/`accounts`/`matching` — mirrors how `apps/classification` is a self-contained, dependency-free engine app.
- **Normalize at write time, not match-time-only**: per explicit user choice. Structured fields are computed once at ingestion (`Job`) and profile-save (`Profile`), not re-derived on every scoring call — keeps `score_job`/`passes_prefilter` cheap and DB-free, and sets up future radius search.
- **Flat `CharField`s on `Job`, a `JSONField` list on `Profile`**: `Job.location` is a single value, so flat `location_city`/`location_region`/`location_country` columns keep it directly filterable in Django admin. `Profile.target_locations` is multi-valued and already a `JSONField` edited as free-text CSV in `ProfileForm` — a parallel `target_locations_normalized` `JSONField` (list of structured dicts) preserves the existing raw-CSV editing UX while adding structure alongside it.
- **`location_alias_version` stamped per-row, mirroring `Job.ruleset_version`**: enables a sweep task to find stale rows on a future alias-table version bump, rather than an ad hoc invalidation mechanism.
- **Structured fields excluded from `_HASH_FIELDS`, included in `_CONTENT_FIELDS`**: they're a pure derivation of the already-hashed raw `location` string plus a deploy-time-constant alias-table version — adding them to `_HASH_FIELDS` would be redundant (they only change when `location` changes, for a fixed alias-table version) and would incorrectly imply an alias-table bump should re-trigger classification, which is an orthogonal concern with its own staleness signal (`location_alias_version`, not `content_hash`).
- **Unresolved locations fall back to substring matching, tagged `resolved=False`**: chosen over a hard no-match/neutral score specifically because global coverage is thin at launch — a no-match default would visibly penalize non-US users for a gap in curation, not their own data. `resolved=False` gives a cheap, queryable coverage signal (admin filter) without building a separate review-queue subsystem.
- **No prefilter gate for location**: `apps/matching/prefilter.py` mirrors its predicates in SQL (`candidate_profiles_for_job`) precisely because its gates are simple indexed-column checks; hierarchy-aware alias matching can't be cheaply mirrored as a SQL predicate without duplicating the alias table into the DB, and doing so would also contradict the deliberate choice to keep location a soft-weighted signal (a job in an adjacent region should still surface, just scored lower), same as salary.
- **Backfill migration uses `.update()`/`bulk_update`, not per-instance `.save()`**: avoids triggering `Profile`'s post-save rematch signal (`apps/matching/signals.py`) or the ingestion `needs_classification` path during a bulk backfill, which could otherwise fan out into an unbounded task storm.

---

## Open Questions

### Resolved During Planning

- Fallback behavior for unresolved locations: substring fallback + `resolved=False` tracking (not no-match/neutral) — see Key Technical Decisions.
- Whether to add alias-table versioning/sweep now vs. defer: include now, as its own implementation unit, to avoid silently-stale data the first time the table is re-curated.
- Whether to add a hard prefilter location gate: no — location stays a soft-scored component only.
- Multi-location postings: parse first segment only in v1, explicit non-goal (see Scope Boundaries).
- Alias-version staleness scope (raised by adversarial review): `CURRENT_LOCATION_ALIAS_VERSION` must be bumped for engine-logic changes that alter resolution output, not only for new curated YAML datasets — otherwise a logic-only bug fix leaves already-normalized rows silently un-reprocessed by the sweep (U8). See U1's Approach.
- Substring-fallback trigger (raised by adversarial review): fallback is keyed on the *job's* location being unresolved, not the profile target's — comparing an unresolved/ambiguous target's raw text against a resolved job reintroduces the original substring false-positive bug. See U7's Approach.

### Deferred to Implementation

- Exact disambiguation precedence for abbreviation collisions (e.g. "GA" as Georgia-US vs. as a country-context hint) beyond the general rule (full comma-context first, then scoped abbreviation lookup) — this is alias-table *content* curation, not scoring logic, and will be refined as real ambiguous cases surface.
- Whether an alias-table version bump should proactively enqueue a full rematch sweep for affected profiles, or fix forward on the next natural rematch trigger — defaults to fix-forward for v1 (see Deferred to Follow-Up Work); revisit if stale-score complaints surface.

---

## Implementation Units

### U1. Location alias/hierarchy engine (`apps/locations`)

**Goal:** A new, dependency-free Django app providing a pure `normalize_location(raw_string) -> dict` function backed by a curated, versioned YAML dataset, following the `apps/classification/engine.py` pattern.

**Requirements:** R1, R6

**Dependencies:** None

**Files:**
- Create: `apps/locations/__init__.py`, `apps/locations/apps.py`
- Create: `apps/locations/geodata/v1.yaml` (curated dataset: US states + abbreviations + major metros with detailed entries; country-level entries for other major markets; `"remote"`-adjacent markers excluded — those stay owned by `_REMOTE_MARKERS`)
- Create: `apps/locations/engine.py` (`load_geodata`, `lru_cache`, `CURRENT_LOCATION_ALIAS_VERSION`, `normalize_location`)
- Create: `apps/locations/tests/__init__.py`, `apps/locations/tests/test_engine.py`
- Modify: `config/settings/base.py` (`INSTALLED_APPS` gains `"apps.locations"`, mirroring the existing `"apps.classification"` entry — without this, the app's management command and tests are not discovered by `manage.py`)

**Approach:**
- `normalize_location(raw)` returns a dict shaped `{"city": str|None, "region": str|None, "country": str|None, "resolved": bool}`. Never raises on `None`/empty/malformed input — an empty or unparseable string returns `{"city": None, "region": None, "country": None, "resolved": False}`, mirroring `_derive_is_remote`'s never-raise contract.
- Cleaning step before lookup: strip, collapse whitespace, casefold, NFKC-normalize (so accented and unaccented forms of the same place can both resolve), strip trailing punctuation. One shared helper, so ingestion and profile-save paths can't drift.
- Strip known remote markers (reuse `apps/jobs/ingestion/normalizers._REMOTE_MARKERS`, or a copy kept in sync via a shared constant) from the string before geo-parsing the remainder, so `"Remote - US"` still resolves `country=US` rather than failing to parse `"Remote"` as a place.
- Multi-location strings (delimiters like `" or "`, `"/"`) parse only the first segment; if that segment alone doesn't resolve, mark `resolved=False`.
- Disambiguation precedence: (1) full comma-separated string resolves most specific-to-least (e.g. `"Atlanta, GA"` — city + state-abbreviation-in-context); (2) a bare, unqualified city name that collides across multiple alias entries (e.g. "Cambridge", "Portland") is *not* silently resolved to a first match — marked `resolved=False` instead; (3) bare abbreviations (e.g. "GA", "IN") are only resolved when a country/region context is already established elsewhere in the string, never as a standalone token; (4) the same non-resolution rule as (2) applies one level up, to bare region/country-name homographs (e.g. "Georgia," which is simultaneously a country and a US state name) — an unqualified bare token that collides across the region/country hierarchy level is marked `resolved=False`, not silently resolved to either meaning.
- `CURRENT_LOCATION_ALIAS_VERSION = "v1"` — bumped both when a new curated `v2.yaml` dataset is promoted **and** whenever a change to `engine.py`'s resolution logic itself (not just the dataset) could change a location's structured output — the version stamp is the only signal the sweep task (U8) uses to find stale rows, so a logic-only fix with no version bump would leave existing rows silently un-reprocessed.

**Patterns to follow:**
- `apps/classification/engine.py` (`load_ruleset`, `lru_cache`, versioned YAML loading, `RuleConfigError`-style error handling)
- `apps/jobs/ingestion/normalizers.py` (`_derive_is_remote`, never-raise-on-bad-input contract)

**Test scenarios:**
- Happy path: `"New York, NY, US"` → `{"city": "New York", "region": "NY", "country": "US", "resolved": True}`.
- Happy path: `"London, UK"` → resolves to city+country (thinner regional data acceptable, country/city must resolve).
- Happy path: `"Germany"` (country-only) → `{"city": None, "region": None, "country": "Germany", "resolved": True}`.
- Edge case: empty string / `None` → `{"city": None, "region": None, "country": None, "resolved": False}`, no exception.
- Edge case: `"Remote - US"` → remote marker stripped, `country="US"` resolved from remainder.
- Edge case: bare ambiguous city `"Cambridge"` (no country/region context) → `resolved=False`.
- Edge case: bare abbreviation with no context `"GA"` alone → `resolved=False` (not silently resolved to Georgia-US or Georgia-country).
- Edge case: `"Atlanta, GA"` → abbreviation resolved via city context → `region="GA"`, `country="US"`.
- Edge case (adversarial finding — region/country homograph): bare `"Georgia"` with no other context (simultaneously a country and a US state name) → `resolved=False`, not silently resolved to either meaning.
- Edge case: unicode place name `"München"` and its unaccented form `"Munich"` → both resolve to the same normalized city.
- Edge case: multi-location `"New York, NY or Remote"` → first segment (`"New York, NY"`) parsed; resolves as if `"New York, NY"` alone.
- Edge case: mixed case / extra whitespace / trailing punctuation (`"  new york,  ny.  "`) → resolves identically to the clean form.
- Edge case: unparseable garbage string (`"asdkfjhasldkfj"`) → `resolved=False`, no exception.

**Verification:**
- `normalize_location` never raises for any string or `None` input.
- Repeated calls with the same input are deterministic (same output every time — no dependence on set/dict iteration order).

---

### U2. `Job` structured location fields (schema)

**Goal:** Add structured location columns to `Job`, no data migration yet.

**Requirements:** R1, R5

**Dependencies:** None

**Files:**
- Modify: `apps/jobs/models.py` (add `location_city`, `location_region`, `location_country` — `CharField(max_length=255, blank=True, default="")`; `location_resolved` — `BooleanField(default=False)`; `location_alias_version` — `CharField(max_length=32, blank=True, default="")`)
- Create: `apps/jobs/migrations/000X_job_structured_location.py` (schema-only, auto-generated)

**Approach:**
- New fields default to blank/unresolved so existing rows remain valid immediately after migration (no backfill in this unit — see U6).
- Add a `models.Index` on `location_resolved` if the admin filter (U8) is expected to run frequently against a large table; otherwise rely on the default filter performance and revisit only if it's slow in practice.

**Test scenarios:**
- Test expectation: none — pure schema addition, no behavioral change. Verified by running the migration cleanly against the existing test DB.

**Verification:**
- Migration applies cleanly; existing `Job` rows load with the new fields at their defaults.

---

### U3. Job ingestion integration

**Goal:** Every newly-ingested or content-changed job gets its structured location fields populated at normalize time, never re-derived downstream.

**Requirements:** R1, R6

**Dependencies:** U1, U2

**Files:**
- Modify: `apps/jobs/ingestion/normalizers.py` (`normalize_job` calls `apps.locations.engine.normalize_location(location_name)`, adds `location_city`, `location_region`, `location_country`, `location_resolved`, `location_alias_version` to the returned dict)
- Modify: `apps/jobs/ingestion/upsert.py` (`_CONTENT_FIELDS` gains the five new field names, so `_apply_content` copies them onto the `Job` row on create/update/reopen)
- Modify: `apps/jobs/tests/test_normalizers.py` (or equivalent existing test file) — new cases for structured field output
- Modify: `apps/jobs/tests/test_upsert.py` (or equivalent) — new cases confirming structured fields land on the row

**Approach:**
- `_HASH_FIELDS` in `apps/jobs/ingestion/config.py` is explicitly **not** changed — structured fields are derived from the already-hashed raw `location`, so no independent staleness signal is needed there.
- Re-ingestion where the raw `location` string is unchanged: `content_hash` matches, `_apply_content` is skipped entirely (existing behavior) — structured fields stay as previously computed, which is correct since nothing about the input changed.
- Re-ingestion where `location` changed: `content_changed=True` already triggers `_apply_content` today; since structured fields are computed inside `normalize_job()` and included in the returned dict, they get recomputed and reapplied automatically — no new code path needed for this case.

**Patterns to follow:**
- `apps/jobs/ingestion/normalizers.py`'s existing `_derive_is_remote` call site — this is a second field derived the same way, right next to it.

**Test scenarios:**
- Happy path: normalize a raw Greenhouse payload with `location.name = "Austin, TX, US"` → normalized dict includes `location_city="Austin"`, `location_region="TX"`, `location_country="US"`, `location_resolved=True`, `location_alias_version="v1"`.
- Edge case: raw payload with missing/empty location → normalized dict has `location_resolved=False`, no exception (mirrors existing empty-location handling for `is_remote`).
- Integration: `upsert_jobs` with a brand-new job — the created `Job` row has structured fields populated (not just the normalized dict, the actual saved row).
- Integration: `upsert_jobs` on an existing job whose raw `location` string changed between fetches — structured fields on the row are recomputed to match the new location, and `needs_classification` is still driven only by the existing `_HASH_FIELDS` (structured field changes alone, if the alias table were hypothetically re-run, must not appear in this code path since it's not exercised here).
- Integration: `upsert_jobs` on an existing job with unchanged raw `location` — structured fields are untouched (row takes the `unchanged_ids` path, `save(update_fields=["scraped_at", "updated_at"])` only).

**Verification:**
- A fresh ingestion run produces `Job` rows with structured location fields consistent with `apps/locations/engine.py`'s `normalize_location` output for their raw `location` string.

---

### U4. `Profile` structured target-location fields (schema)

**Goal:** Add structured storage for normalized target locations to `Profile`, alongside the existing raw CSV-editable list.

**Requirements:** R1, R5

**Dependencies:** None

**Files:**
- Modify: `apps/accounts/models.py` (add `target_locations_normalized` — `JSONField(default=list, blank=True)`, each entry shaped `{"raw": str, "city": str|None, "region": str|None, "country": str|None, "resolved": bool}`; add `target_locations_alias_version` — `CharField(max_length=32, blank=True, default="")`)
- Create: `apps/accounts/migrations/000X_profile_structured_locations.py` (schema-only, auto-generated)

**Approach:**
- `target_locations` (raw strings) is unchanged — it remains the field `ProfileForm` reads/writes for the comma-separated text input. `target_locations_normalized` is a derived, parallel field, recomputed whenever `target_locations` changes.
- New fields default to empty/blank so existing `Profile` rows remain valid immediately (no backfill in this unit — see U6).

**Test scenarios:**
- Test expectation: none — pure schema addition, no behavioral change. Verified by running the migration cleanly.

**Verification:**
- Migration applies cleanly; existing `Profile` rows load with the new fields at their defaults.

---

### U5. `ProfileForm` normalization integration

**Goal:** Every profile save recomputes `target_locations_normalized` from the current `target_locations` list.

**Requirements:** R1, R6

**Dependencies:** U1, U4

**Files:**
- Modify: `apps/web/forms.py` (`ProfileForm.clean_target_locations`, or a `save()` override, computes `target_locations_normalized` from the cleaned CSV list via `apps.locations.engine.normalize_location`, and stamps `target_locations_alias_version`)
- Modify: `apps/web/tests/test_forms.py` (or equivalent) — new cases for normalization on save

**Approach:**
- Each entry in the cleaned `target_locations` list is normalized independently; the resulting list preserves order and 1:1 correspondence with the raw list (`{"raw": entry, ...normalize_location(entry)}`).
- Deduplicate on the *normalized* tuple (city, region, country), not the raw string, when building `target_locations_normalized` — so `"NYC"` and `"New York, NY"` typed together don't double-count in hierarchy matching. The raw `target_locations` list itself is left as the user typed it (not deduplicated) so the editable CSV field round-trips exactly what they entered.
- Unresolved entries (`resolved=False`) are still stored in `target_locations_normalized` (not dropped) — scoring treats them as inert (see U7), and their presence is what lets a future UI surface "location not recognized" without needing a second lookup pass.

**Patterns to follow:**
- `apps/web/forms.py`'s existing `_clean_list`/`clean_target_locations` structure — extend it, don't replace the CSV-splitting behavior.

**Test scenarios:**
- Happy path: submitting `target_locations` CSV `"New York, London"` → `target_locations_normalized` has two entries, both `resolved=True` with correct structured fields, `target_locations_alias_version` set to current version.
- Edge case: empty `target_locations` (no locations entered) → `target_locations_normalized` is `[]`, unchanged from today's empty-list behavior.
- Edge case: CSV list mixing a resolvable and an unresolvable entry (`"New York, Xyzzyville"`) → both entries present in `target_locations_normalized`, one `resolved=True`, one `resolved=False` — form save does not reject or error on the unresolved entry.
- Edge case: CSV list with duplicate-after-normalization entries (`"NYC, New York"`) → `target_locations_normalized` contains only one entry for that structured location.
- Integration: saving the profile form end-to-end (`ProfileForm(data, instance=profile).save()`) persists both `target_locations` (raw) and `target_locations_normalized` (structured) correctly on the `Profile` row.

**Verification:**
- Editing and re-saving a profile's target locations always leaves `target_locations_normalized` consistent with the current `target_locations` content and the current alias-table version.

---

### U6. Backfill data migration

**Goal:** Normalize existing `Job.location` and `Profile.target_locations` data into the new structured fields — the repo's first `RunPython` data migration.

**Requirements:** R1, R4

**Dependencies:** U1, U2, U3, U4, U5 (U3/U5 must already be deployed — see race-safety note below)

**Files:**
- Create: `apps/locations/services.py` (`backfill_jobs(Job, batch_size=...)`, `backfill_profiles(Profile, batch_size=...)` — importable, testable functions that accept the model class as a parameter rather than importing it directly, so the same function is safe to call with either a migration's historical model (`apps.get_model(...)`) or the live model class; each queries rows where `location_alias_version != CURRENT_LOCATION_ALIAS_VERSION` (or, for `Profile`, `target_locations_alias_version != CURRENT_LOCATION_ALIAS_VERSION`), normalizes in batches, and writes with a version-guarded conditional update rather than per-instance `.save()` — see race-safety note below)
- Create: `apps/jobs/migrations/000X_backfill_job_locations.py` (`RunPython` resolving `Job` via `apps.get_model("jobs", "Job")` per Django's historical-model convention for data migrations, then calling `apps.locations.services.backfill_jobs`, with a no-op reverse)
- Create: `apps/accounts/migrations/000X_backfill_profile_locations.py` (`RunPython` resolving `Profile` via `apps.get_model("accounts", "Profile")`, then calling `apps.locations.services.backfill_profiles`, with a no-op reverse)
- Create: `apps/locations/management/commands/backfill_locations.py` (thin `Command` importing the live `Job`/`Profile` models and passing them into the same service functions, for manual re-run outside the migration)
- Create: `apps/locations/tests/test_services.py`

**Approach:**
- **Race safety (the actual write-time guard, not just the read-time filter):** the read-time filter (`location_alias_version != CURRENT`) only decides which rows this batch *attempts*; it does not by itself prevent a lost update. Between this migration reading a row and writing its computed structured fields, concurrent ingestion (U3) or a profile save (U5) may have already re-saved that exact row with fresh data and the current version stamp. Each write in the batch must therefore be a **conditional update guarded on the row's `location_alias_version` still equal to the value read at batch-fetch time** (e.g. `UPDATE ... SET location_city=..., location_alias_version=<current> WHERE id=<id> AND location_alias_version=<version_seen_at_read>`) — if a concurrent writer already advanced the row past that version, this migration's write for that row affects zero rows and is silently skipped rather than overwriting the fresher data. This is what makes "idempotent and safely interleavable" true at the write level, not just at the read-filter level.
- Idempotency by construction: filtering on `location_alias_version != CURRENT_LOCATION_ALIAS_VERSION` means a row already normalized under the current version is skipped, whether it got there via this migration, via concurrent ingestion/profile-save (U3/U5, already deployed by the time this migration runs), or a prior partial run of this same migration. Combined with the write-time guard above, a row already advanced by a concurrent writer is never regressed.
- Batches sized via a new `LOCATION_BACKFILL_BATCH_SIZE` setting (mirrors `CLASSIFICATION_BATCH_SIZE`), so a large `Job` table doesn't get processed in one unbounded query.
- Per Django's documented data-migration convention, the `RunPython` operations resolve `Job`/`Profile` through the migration's historical `apps` registry (`apps.get_model(...)`), not a live import — a live import can silently diverge from the schema a migration replay expects if a later migration renames or removes a field this backfill relies on. `apps/locations/services.py`'s functions accept the model class as an argument specifically so they work correctly from both the historical-model migration call site and the live-model management command call site.
- Writes use `.update()`/`bulk_update` (not `Model.save()`), so this does not trigger `Profile`'s post-save rematch signal or re-flag `needs_classification` on `Job` — this backfill changes derived location structure, not content that scoring/classification should re-run over on its own (scoring picks up the new structured fields the next time it runs naturally).
- Migration `RunPython` operations call the service functions directly (no logic embedded in the migration file itself), matching `add_job_source.py`'s command-delegates-to-service split — so the same backfill logic is exercised by tests without a full migration run.

**Execution note:** Test-first for `backfill_jobs`/`backfill_profiles` — these are the highest-risk new code path in this plan (first data migration in the repo) and should have failing tests for idempotency and batching before implementation.

**Test scenarios:**
- Happy path: `backfill_jobs` on a table with unnormalized `Job` rows (blank `location_alias_version`) → all rows get structured fields matching `normalize_location(job.location)`, and `location_alias_version` set to current.
- Happy path: `backfill_profiles` analogous, over `Profile.target_locations`.
- Edge case: a row already at the current `location_alias_version` (written by already-deployed ingestion/profile-save code, i.e. U3/U5) is skipped — not reprocessed, not overwritten.
- Edge case: running `backfill_jobs` twice in a row (simulating a re-run after partial failure) is a no-op the second time — same end state, no errors.
- Edge case: batch size smaller than the total row count — all rows still get processed across multiple internal batches, none skipped.
- Edge case (race safety): a row's `location_alias_version` is advanced by a concurrent write (simulate by updating the row between `backfill_jobs`'s read and write phases) — the backfill's write for that row is a no-op (conditional update affects zero rows), and the concurrently-written fresh data survives untouched.
- Integration: after `backfill_jobs` runs, no `needs_classification` flags changed and no classification/matching Celery tasks were enqueued as a side effect (writes bypass `.save()`/signals).
- Integration: after `backfill_profiles` runs, no rematch tasks were enqueued as a side effect.

**Verification:**
- Running the migration against a database with pre-existing `Job`/`Profile` rows (created before this plan's schema changes) leaves every row with `location_alias_version == CURRENT_LOCATION_ALIAS_VERSION` and structured fields consistent with `normalize_location` output.
- Running the migration a second time (or the management command manually) is a safe no-op.
- A row concurrently advanced mid-backfill is never regressed to stale data (the write-time version guard holds).

---

### U7. Scoring rewrite — hierarchy matching + `ONSITE_ONLY` bug fix

**Goal:** `_location_component()` compares structured fields with hierarchy-aware matching (falling back to substring for unresolved locations), and correctly checks `target_locations` for `ONSITE_ONLY` profiles.

**Requirements:** R1, R2, R3, R6

**Dependencies:** U1, U2, U4 (needs the structured fields to exist; can be built/tested independently of U3/U5/U6 landing first, using the test factories directly)

**Files:**
- Modify: `apps/matching/services.py` (`profile_snapshot` adds `target_locations_normalized`; `job_snapshot` adds `location_city`, `location_region`, `location_country`, `location_resolved`)
- Modify: `apps/matching/scoring.py` (`_location_component` rewritten)
- Modify: `apps/matching/tests/factories.py` (`make_job`/`make_profile` accept the new structured kwargs, with sensible defaults)
- Modify: `apps/matching/tests/test_scoring.py` (new/updated cases, including a direct regression test for the `ONSITE_ONLY` bug)

**Technical design:**

> This illustrates the intended approach and is directional guidance for review, not implementation specification.

```
_location_component(profile, job):
    if remote_pref == REMOTE_ONLY:
        return 1.0 if job.is_remote else 0.0        # unchanged — no location check
    if remote_pref == ONSITE_ONLY:
        if job.is_remote: return 0.0                 # unchanged
        return _match_targets(profile, job)           # CHANGED — was unconditional 1.0
    # ANY
    if job.is_remote: return 1.0                      # unchanged
    return _match_targets(profile, job)                # unchanged call site, new implementation

_match_targets(profile, job):
    targets = profile["target_locations_normalized"]
    if not targets: return 1.0                         # no constraint stated — unchanged semantic
    if any(_hierarchy_match(t, job) for t in targets if t["resolved"]):
        return 1.0
    if job["location_resolved"]:
        return 0.0   # job is structured; an unresolved/ambiguous target (e.g. bare "NY") must
                      # NOT fall back to raw substring against a resolved job — that reintroduces
                      # the exact false-positive this plan fixes ("ny" inside "Albany")
    return _substring_fallback(profile["target_locations"], job["location"])  # job itself unresolved

_hierarchy_match(target, job):
    # unset levels on the target are wildcards — only compare levels the user actually specified
    if target["city"] and target["city"] != job["location_city"]: return False
    if target["region"] and target["region"] != job["location_region"]: return False
    if target["country"] and target["country"] != job["location_country"]: return False
    return True  # at least one level was specified and none of the specified ones mismatched
```

**Approach:**
- `_match_targets` is shared between the `ONSITE_ONLY` and `ANY` branches — this is the concrete fix for the bug (both branches now route through the same target-checking logic; previously only `ANY` did).
- Substring fallback applies **only when the job's own location is unresolved** (the alias table has no structured data for it at all) — not merely when a profile target is unresolved. A profile target can be unresolved because it's genuinely ambiguous or malformed (e.g. a bare state abbreviation like `"NY"` with no city/country context, which `apps/locations`'s disambiguation rules deliberately refuse to resolve standalone); substring-comparing that raw text against a *resolved* job's location string reproduces the exact bug this plan exists to fix (`"ny"` as a substring of `"albany"`). When the job is resolved but no target hierarchy-matches, the correct answer is "no match" (`0.0`), not a raw-text fallback. Substring fallback stays reserved for the actual thin-coverage case it was designed for: a job location the alias table simply hasn't curated yet.
- Wildcard semantics are the crux of hierarchy matching and easy to get backwards: a target with only `region` set (e.g. "California") matches any job with `location_region == "California"` regardless of city — it does not require `location_city` to also be unset or matching.
- `matching_tags`-style purity is preserved: no DB access, `_location_component` still operates only on snapshot dict fields.

**Patterns to follow:**
- `_salary_component`'s "unknown is neutral, never silently dropped" precedent — same posture for unresolved locations (fallback, not automatic zero).
- Existing `_location_component` structure (remote_pref branching) — extend, don't restructure beyond what's needed.

**Test scenarios:**
- Happy path (regression, the reported bug): `ONSITE_ONLY` profile with `target_locations_normalized=[{"city": None, "region": None, "country": "US", "resolved": True}]` (i.e. targeting "US") scores full location credit for an onsite US job and zero for an onsite job in another country — confirms `target_locations` is no longer ignored for `ONSITE_ONLY`.
- Happy path: `ANY` profile with a resolved city-level target matches a job with the same `location_city`, regardless of unrelated jobs at other cities in the same region.
- Happy path: `REMOTE_ONLY` profile continues to ignore `target_locations` entirely — unchanged from today, explicit regression test.
- Edge case: profile target specifies only `region` ("California") — matches jobs with that region regardless of city (wildcard semantics).
- Edge case: profile target specifies only `country` — matches any job in that country, thin non-US coverage case (country resolves even when city/region don't).
- Edge case: empty `target_locations_normalized` → `1.0` (no constraint), unchanged from today's "no constraint stated" behavior.
- Edge case: profile's `target_locations_normalized` has an unresolved entry alongside a resolved one, job is resolved — the unresolved entry is inert (never itself produces a match), the resolved entry still can.
- Edge case (regression, adversarial finding): profile target is a bare, unqualified abbreviation like `"NY"` (marked `resolved=False` by `apps/locations`'s disambiguation rules) and the job resolves to `location_city="Albany", location_region="NY"` — score is `0.0`, NOT a substring-fallback match on `"ny" in "albany"`. This is the regression test for the exact false-positive named in the Problem Frame.
- Edge case: job's location is unresolved (not yet curated) and every profile target is also unresolved → falls back to substring matching against raw strings, same result as pre-fix behavior for that pair (the deliberate thin-coverage fallback).
- Edge case: job's location is unresolved but a profile target is resolved → falls back to substring matching (job side unresolved is the only trigger for fallback; not treated as a guaranteed mismatch).
- Integration: `apps/matching/services.py`'s `match_job`/`rematch_profile_obj` produce a `UserJobMatch` whose `match_score`/`match_status` reflect the corrected location component end-to-end (not just the pure function in isolation).

**Verification:**
- `test_remote_only_profile_vs_onsite_job_scores_zero_location` (existing test) still passes unchanged.
- A new test directly reproducing the originally-reported bug (`ONSITE_ONLY` + `target_locations` set + non-matching-location onsite job) scores strictly lower than a matching-location onsite job — this is the plan's core acceptance check.

---

### U8. Alias-version sweep + admin visibility

**Goal:** Future alias-table version bumps get a defined re-normalization trigger, and unresolved locations are visible to whoever curates the dataset.

**Requirements:** R5

**Dependencies:** U1, U2, U4, U6

**Files:**
- Create: `apps/locations/tasks.py` (`sweep_stale_locations` — Celery task, batched, mirroring `apps/classification/tasks.py`'s `sweep_unclassified`; calls the same `backfill_jobs`/`backfill_profiles` service functions from U6)
- Modify: `config/settings/base.py` (`CELERY_BEAT_SCHEDULE` gains a `sweep_stale_locations` entry; `LOCATION_BACKFILL_BATCH_SIZE` env-driven setting, reused by both U6 and this sweep)
- Modify: `apps/jobs/admin.py` (`JobAdmin.list_filter` gains `location_resolved`)
- Modify: `apps/accounts/admin.py` (`ProfileAdmin` already exists — extend its `list_filter`/queryset with a filter equivalent for profiles that have at least one unresolved entry in `target_locations_normalized`, e.g. a `Postgres` `contains`-style lookup for `{"resolved": false}` in the list)
- Create: `apps/locations/tests/test_tasks.py`

**Approach:**
- `sweep_stale_locations` is functionally identical to running U6's backfill again — it exists so a *future* alias-table version bump (not this plan's initial rollout) has a scheduled, bounded, automatic path to re-normalize affected rows, the same way `sweep_unclassified` catches anything the event-driven classification path missed.
- No new signal or event-driven trigger is added for alias-table bumps (that's a manual/deploy-time event, not a per-row event) — the periodic sweep is sufficient, consistent with how `ruleset_version` bumps are handled today (sweep-only, no dedicated event).

**Test scenarios:**
- Happy path: bumping `CURRENT_LOCATION_ALIAS_VERSION` (simulated in a test) and running `sweep_stale_locations` re-normalizes previously-normalized rows whose stamped version no longer matches current.
- Edge case: sweep run with no stale rows (nothing to do) → no-op, no errors, bounded query cost (doesn't scan more than necessary).
- Edge case: sweep run bounded by `LOCATION_BACKFILL_BATCH_SIZE` — a large stale-row count is processed across multiple sweep invocations, not all at once.
- Integration: `Job.objects.filter(location_resolved=False)` is usable and correctly filtered in the Django admin list view.

**Verification:**
- After a simulated alias-table version bump, one or more sweep runs converge every row to `location_alias_version == CURRENT_LOCATION_ALIAS_VERSION`.
- The admin `location_resolved=False` filter surfaces exactly the jobs whose location didn't resolve, for manual curation prioritization.

---

## System-Wide Impact

- **Interaction graph:** `apps/jobs/ingestion` (normalize → upsert) and `apps/web/forms.py` (profile save) both gain a dependency on the new `apps/locations` engine. `apps/matching/services.py`'s snapshot builders gain two new structured-field reads. A new Celery Beat entry (`sweep_stale_locations`) joins the existing hourly/off-peak/five-minute schedule.
- **Error propagation:** `normalize_location` never raises — matches the existing contract of `_derive_is_remote` — so no new exception path is introduced into ingestion, profile-save, or scoring.
- **State lifecycle risks:** the backfill migration (U6) is the primary risk surface — mitigated by version-stamp filtering (idempotent, safely interleavable with concurrent writes) and by using `.update()`/`bulk_update` to avoid triggering rematch/classification signals as a side effect of a bulk data migration.
- **API surface parity:** none — no public API in this app; all changes are internal (models, ingestion, forms, scoring, admin).
- **Integration coverage:** end-to-end tests confirmed at U3 (ingestion → structured `Job` row), U5 (form save → structured `Profile` fields), U6 (backfill → no signal side effects), and U7 (`match_job`/`rematch_profile_obj` → corrected `UserJobMatch` scores) — each unit's test scenarios include at least one cross-layer case, not just pure-function coverage.
- **Unchanged invariants:** `apps/matching/prefilter.py` gains no new gate — location remains soft-scored only. `TAG_WEIGHT`/`TITLE_WEIGHT`/`SALARY_WEIGHT`/`LOCATION_WEIGHT` and `MATCH_SCORE_THRESHOLD` in `apps/matching/constants.py` are unchanged. `is_remote`/`remote_pref` remain the sole remote-vs-onsite axis; this plan does not touch remote-detection logic (`_derive_is_remote`/`_REMOTE_MARKERS`) beyond reusing the marker list for stripping before geo-parsing.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| First-ever `RunPython` data migration in the repo — a naive blanket `UPDATE` could lock or time out on a large `Job` table | U6 filters to unnormalized/stale rows only, batches via `LOCATION_BACKFILL_BATCH_SIZE`, and delegates to an idempotent, independently-testable service function reused by both the migration and a manual management command |
| Ambiguous bare-city names or abbreviation collisions could silently mis-resolve a location, producing confidently-wrong (not just imprecise) scores | U1's disambiguation precedence rules mark ambiguous/unqualified matches `resolved=False` rather than guessing; substring fallback (not a wrong structured match) is the result |
| Alias-table curation is ongoing manual effort — coverage gaps could persist indefinitely without visibility | `location_resolved=False` admin filter (U8) surfaces the unresolved rate directly; substring fallback (R3) prevents a scoring cliff in the meantime |
| Thin non-US coverage at launch could visibly worsen recommendations for non-US users if the wrong fallback were chosen | Substring fallback (not no-match/neutral) was chosen specifically to avoid this — see Key Technical Decisions |
| Backfill migration accidentally triggers a rematch/classification storm via model signals | U6 explicitly uses `.update()`/`bulk_update`, bypassing `Model.save()` and the signals it fires |

---

## Documentation / Operational Notes

- New env var `LOCATION_BACKFILL_BATCH_SIZE` (mirrors `CLASSIFICATION_BATCH_SIZE`), consumed by both the backfill migration's service functions (U6) and the sweep task (U8).
- New Celery Beat schedule entry for `sweep_stale_locations` — cadence should mirror `sweep_unclassified`'s "catch anything missed" framing (e.g. every few minutes) since it's a cheap no-op when there's nothing stale.
- Whenever `apps/locations/geodata/` gets a new curated version (`v2.yaml`, ...), bump `CURRENT_LOCATION_ALIAS_VERSION` — the sweep (U8) picks up re-normalization automatically; no manual per-row action needed.
- Good `/ce-compound` candidate once shipped: the alias-table-vs-substring rationale, the version-stamped-sweep pattern, and the `ONSITE_ONLY` bug are all reusable lessons for a repo with no `docs/solutions/` yet.

---

## Sources & References

- No formal origin requirements document — planned directly from a `/ce-debug` investigation (confirmed the `ONSITE_ONLY` bug) followed by a `/ce-brainstorm` dialogue (confirmed global scope, write-time normalization, and the alias/hierarchy-table approach) in this session.
- Related code: `apps/matching/scoring.py`, `apps/matching/prefilter.py`, `apps/matching/services.py`, `apps/jobs/ingestion/normalizers.py`, `apps/jobs/ingestion/upsert.py`, `apps/jobs/ingestion/config.py`, `apps/accounts/models.py`, `apps/web/forms.py`, `apps/classification/engine.py`, `apps/classification/tasks.py`.
