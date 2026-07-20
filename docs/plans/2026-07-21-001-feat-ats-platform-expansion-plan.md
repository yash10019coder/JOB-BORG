---
title: ATS Platform Expansion (Lever, Ashby, Workday)
type: feat
status: completed
date: 2026-07-21
origin: docs/brainstorms/2026-07-21-ats-platform-expansion-requirements.md
deepened: 2026-07-21
---

# ATS Platform Expansion (Lever, Ashby, Workday)

## Summary

Introduce an ATS-keyed dispatch registry so `ingest_source`, `discover_boards`, `register_job_source`, and the `add_job_source` management command stop hardcoding `GreenhouseClient`, then add `LeverClient` and `AshbyClient` (mirroring `greenhouse_client.py`'s retry/error pattern) as the first platforms to flow through it. Workday follows as a second phase: a vendored copy of jobhive's `WorkdayScraper` source (not a `jobhive-py` pip dependency, to avoid an unnecessary `pandas` transitive dependency) behind an adapter that produces the same normalized job dict every other client produces. A shared exception base class lets dispatch/admin code catch failures generically instead of listing every ATS's exception classes by name.

---

## Problem Frame

`apps/jobs/tasks.py::ingest_source` and `discover_boards` — plus `apps/jobs/ingestion/register.py::register_job_source` and the `add_job_source` management command — all instantiate `GreenhouseClient` directly rather than dispatching on `JobSource.ats` / `DiscoveredBoard.ats`, even though both models already model the field as a `TextChoices` enum built for multiple platforms. `apps/jobs/ingestion/exceptions.py` and `apps/jobs/ingestion/normalizers.py` are similarly Greenhouse-named though partly reusable. Job coverage is capped at one ATS as a result (see origin document for full problem framing and why Lever, Ashby, and Workday specifically).

---

## Requirements

- R1. `ingest_source` selects the client implementation based on `JobSource.ats` instead of always instantiating `GreenhouseClient`.
- R2. `discover_boards` selects the discovery/validation client based on the ATS being discovered instead of always instantiating `GreenhouseClient`.
- R3. Adding a new ATS does not require changes to upsert/classification logic downstream of client fetch + normalize.
- R4. A `LeverClient` and an `AshbyClient` fetch jobs from each platform's public per-company board API, following the retry/error-handling pattern in `apps/jobs/ingestion/greenhouse_client.py`.
- R5. Each platform's raw job payload is normalized into the same dict shape `normalize_job()` produces for Greenhouse.
- R6. `ATS.LEVER` and `ATS.ASHBY` are added to the `JobSource`/`DiscoveredBoard` `ats` choices.
- R7. Discovery finds candidate Lever and Ashby boards using the same CSV-dataset mechanism `board_search.py` uses for Greenhouse.
- R8. Candidate Lever and Ashby boards go through the same `DiscoveredBoard` pending-review-and-approval flow Greenhouse candidates go through.
- R9. Workday job fetching is implemented by vendoring jobhive's `WorkdayScraper` rather than reimplementing its pagination/facet-subdivision logic.
- R10. An adapter maps the vendored scraper's job output to the same normalized dict shape as other platforms.
- R11. `ATS.WORKDAY` is added to the `ats` choices.
- R12. `discover_boards` discovers candidate Workday tenants using the same CSV-dataset mechanism, going through the same `DiscoveredBoard` review flow.

**Origin acceptance examples:** AE1 (covers R1, R3), AE2 (covers R8), AE3 (covers R9, R10)

---

## Scope Boundaries

- LinkedIn and Indeed are out of scope — tracked in separate issues (#22, #23).
- Hand-rolling Workday's own pagination/facet-subdivision logic is out of scope — vendoring jobhive's implementation instead.
- Changes to the `DiscoveredBoard` review UI/UX are out of scope — the existing review mechanism is reused as-is.

### Deferred to Follow-Up Work

- Any further ATS platform beyond Lever/Ashby/Workday: separate future work, enabled by the dispatch registry this plan introduces.

---

## Context & Research

### Relevant Code and Patterns

- `apps/jobs/ingestion/greenhouse_client.py` — the client pattern to mirror: constructor (`session`, `max_retries=3`, `backoff_factor=0.5`, `timeout=10`, `sleep=time.sleep`), `fetch_jobs(board_token)`, `_get_with_retry` (retries on `{429, 500, 502, 503, 504}` and network exceptions, exponential backoff, honors `Retry-After`), `_parse_body` (raises on non-JSON).
- `apps/jobs/ingestion/normalizers.py::normalize_job` — Greenhouse-specific despite the generic name; the exact output dict shape (`source_ats`, `source_job_id`, `title`, `description`, `location`, `is_remote`, `location_city`/`region`/`country`, `location_resolved`, `location_alias_version`, `salary_min`/`max`, `source_url`) is the contract every new normalizer must match.
- `apps/jobs/ingestion/exceptions.py` — `GreenhouseError` / `GreenhouseUnavailable` / `GreenhouseParseError`, no shared base today.
- `apps/jobs/ingestion/upsert.py::upsert_jobs` — already fully ATS-agnostic (keys off `source_ats`/`source_job_id` from the normalized dict, sets `Job.employer` from the `JobSource` FK, computes `content_hash` from normalized fields). No changes needed here — confirms R3.
- `apps/jobs/ingestion/register.py::register_job_source` — the shared validate-then-persist path used by both the `add_job_source` management command and `DiscoveredBoardAdmin.approve`. Currently hardcoded to `GreenhouseClient` and `JobSource.ATS.GREENHOUSE` — a third and fourth hardcoding site beyond `tasks.py`, in scope for this plan since it's the same problem R1/R2 target.
- `apps/jobs/ingestion/board_search.py` — `BoardSearchClient.search_greenhouse_boards()` is a single ATS-specific method pulling `greenhouse.csv` from the `kalil0321/ats-scrapers` dataset; needs to generalize to a per-ATS CSV lookup.
- `apps/jobs/admin.py::DiscoveredBoardAdmin.approve` — calls `register_job_source(board.board_token, employer_name=...)` without passing `board.ats`, and catches `(GreenhouseUnavailable, GreenhouseParseError)` by name. `JobSourceAdmin`/`DiscoveredBoardAdmin`'s `list_filter = (..., "ats", ...)` already works generically off the `TextChoices` enum — no change needed there.
- `apps/jobs/management/commands/add_job_source.py` — hardcodes `GreenhouseClient()`, no `--ats` flag.
- `apps/jobs/models.py::JobSource.board_token` — plain `CharField(max_length=255)`, no format constraint. Confirmed sufficient to store Workday's full careers URL (`https://{company}.{instance}.myworkdayjobs.com/{site}`) without a schema change.
- `apps/jobs/tests/test_greenhouse_client.py` — the test template to mirror for `LeverClient`/`AshbyClient`: `SimpleTestCase` + `@responses.activate`, a `_client()` helper with `max_retries=2, backoff_factor=0, sleep=lambda _s: None` for instant retry tests, covering happy path, malformed JSON, missing key, transient-then-recover, exhausted retries, `Retry-After` honored, non-retryable 4xx, network timeout.
- `apps/jobs/tests/test_discover_boards.py` — mocks `BoardSearchClient`/`GreenhouseClient` via hand-rolled fakes rather than `responses`, testing task orchestration (skip-known-tokens, create-pending-board, discard-invalid, per-source failure isolation, discovery cap).
- `apps/jobs/tests/test_admin_discovered_board.py` and `test_register_job_source.py` — approve/reject flow and `register_job_source` behavior, both currently Greenhouse-only.

### Institutional Learnings

- `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md` (2026-07-19, high severity): raw substring/text comparison on location strings produces false positives (e.g., "NY" matching "Albany"). Every new normalizer must route the raw location string through `apps.locations.normalize_location` rather than writing per-source parsing — this is the exact bug class that fix eliminated. `apps/locations` is deliberately dependency-free so ingestion code can call into it directly.

### External References

- `github.com/kalil0321/ats-scrapers` (`jobhive-py`) — read directly (not via docs) during brainstorming: `src/jobhive/scrapers/workday.py` (609 lines, pagination-cap + facet-subdivision handling), `src/jobhive/scrapers/base.py` (`BaseScraper`/`ScraperRegistry`), `src/jobhive/models.py` (`Job` pydantic model). `src/jobhive/__init__.py` unconditionally imports `jobhive.client`, which imports `pandas` — confirmed this makes `pip install jobhive-py` + `import jobhive.scrapers.workday` pull in `pandas` regardless of which submodule is used, since Python always executes a package's `__init__.py` on any submodule import. Direct source vendoring (copying the scraper file(s) instead of depending on the package) avoids this.

---

## Key Technical Decisions

- **Shared ingestion exception base classes** (`IngestionError` / `IngestionUnavailable` / `IngestionParseError` in `exceptions.py`, with each platform's existing/new exception classes inheriting from them): lets dispatch and admin code catch failures generically (`except (IngestionUnavailable, IngestionParseError)`) instead of enumerating every ATS's exception classes by name at every call site — required for R3's intent to extend cleanly.
- **Dict-based client dispatch registry** in a new `apps/jobs/ingestion/dispatch.py`, keyed by `JobSource.ATS` value: no dispatch/registry pattern exists yet in this codebase; a plain dict mapping enum value → client class is the simplest mechanism consistent with the existing `TextChoices` declarations.
- **Workday vendored via direct source copy, not a `jobhive-py` pip dependency**: avoids the transitive `pandas` dependency (see External References above). `httpx` and `pydantic` are still required since the vendored `WorkdayScraper` is async/httpx-based and its `Job` model is pydantic.
- **`board_token` reused for Workday's full careers URL** (no new field, no schema change) — documented in code as a per-ATS convention: a short slug for Greenhouse/Lever/Ashby, a full URL for Workday.
- **`board_search.py` generalized to `search_boards(ats)`** with a per-ATS CSV filename mapping, replacing the single `search_greenhouse_boards()` method. The stray module-level self-invoking code is removed in the same change since it directly blocks safely importing the module this plan modifies.
- **`register_job_source` and `add_job_source` gain an `ats` parameter/flag** (default `GREENHOUSE` to avoid breaking any other untouched callers), closing hardcoding sites research found beyond the two named in the origin document.
- **`register_job_source` derives `Employer.slug` from `slugify(employer_name)` instead of the raw `board_token`**: doc review found `register_job_source` currently does `Employer.objects.get_or_create(slug=token, ...)`, using the token verbatim as the slug. This is a latent bug the moment `board_token` stops being a short, slug-safe string — which is exactly what happens for Workday, where `board_token` is a full URL. It also means two different companies on different platforms that happen to choose the same short token (e.g. `careers`) would silently collapse onto one `Employer` row via the unique `slug` field, misattributing jobs. Deriving from the company name instead (matching `Employer.save()`'s own existing `slugify(self.name)` fallback) fixes both: the URL never reaches `slug`, and identity is keyed on company name rather than an ATS-specific token. This is a real behavior change for already-registered-going-forward Greenhouse/Lever/Ashby employers too (existing rows are unaffected — `get_or_create` only sets `slug` on creation), not just a Workday fix.
- **Workday's `board_token` is validated against an allowed hostname pattern before any fetch, both at discovery time and at approval time**: doc review found that because `board_token` is itself the fetch destination for Workday (unlike the other three platforms, where the token is a path segment on a fixed first-party host), and `discover_boards` already fetches candidate tokens server-side to validate them *before* any reviewer sees them, an unvalidated Workday token is a server-side-request-forgery surface — a corrupted or malicious entry in the third-party discovery dataset (or a manually submitted candidate) could point the unattended daily sweep at an arbitrary internal address. `WorkdayClient` rejects any `board_token` that doesn't match the `https://{company}.{instance}.myworkdayjobs.com/{site}` pattern (the same pattern the vendored scraper already parses `company`/`instance`/`site` out of) before issuing any request, closing this off at the one chokepoint every Workday fetch already passes through.

---

## Open Questions

### Resolved During Planning

- Should `ats` choices be added incrementally or all at once? Incrementally — `ATS.LEVER`/`ATS.ASHBY` land in Phase 1's migration, `ATS.WORKDAY` in Phase 2's.
- Can jobhive's `WorkdayScraper` be used without pulling in `pandas`? Yes — vendor by copying the scraper source directly rather than depending on the `jobhive-py` package; `httpx` and `pydantic` are still required.
- Does Workday's board identifier need a new field? No — `JobSource.board_token` is an unconstrained `CharField(255)` and can store the full careers URL directly.
- Exact set of jobhive source files needed to vendor `WorkdayScraper` cleanly: confirmed by reading `workday.py`'s own imports directly. It needs only `BaseScraper` (from `base.py` — `ScraperRegistry` is not needed, since our own `dispatch.py` replaces it), the `Job`/`Salary`/`EmploymentType`/`SalaryPeriod` pydantic models (from `models.py` — `Job`'s field definitions are plain `pydantic.Field` declarations with no cross-module validators, so it vendors cleanly on its own), and a trimmed `exceptions.py` (`JobHiveError`, `ScraperError`, `CompanyNotFoundError` only — `ManifestError`/`StorageError` are dataset-client-only and not needed).

### Deferred to Implementation

- Whether the vendored `WorkdayScraper`'s `@ScraperRegistry.register(...)` decorator is simply dropped (since our `dispatch.py` doesn't use jobhive's registry) or the vendored `base.py` omits `ScraperRegistry` entirely — a small edit either way, left for implementation.
- Exact Lever Postings API (`https://api.lever.co/v0/postings/{company}?mode=json`) and Ashby Job Board API (`https://api.ashbyhq.com/posting-api/job-board/{boardName}`) response field names — confirm against a live fetch before finalizing each normalizer; the mapping in this plan is directional.

---

## Implementation Units

### U1. Shared ingestion exception hierarchy

**Goal:** Let dispatch and admin code catch ingestion failures generically across ATS platforms.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `apps/jobs/ingestion/exceptions.py`
- Test: `apps/jobs/tests/test_greenhouse_client.py` (verify existing Greenhouse exception behavior is unchanged after the base-class refactor)

**Approach:**
- Add `IngestionError(Exception)`, `IngestionUnavailable(IngestionError)`, `IngestionParseError(IngestionError)`.
- Change `GreenhouseError` to inherit from `IngestionError`; `GreenhouseUnavailable`/`GreenhouseParseError` additionally inherit from `IngestionUnavailable`/`IngestionParseError` respectively (multiple inheritance), so existing `except GreenhouseUnavailable` call sites keep working unchanged while new generic call sites can catch the base classes.

**Patterns to follow:**
- Existing three-class-per-ATS shape in `apps/jobs/ingestion/exceptions.py`.

**Test scenarios:**
- Happy path: existing Greenhouse exception tests continue to pass unmodified.
- Edge case: a `GreenhouseUnavailable` instance is also an instance of `IngestionUnavailable` (and `GreenhouseParseError` of `IngestionParseError`).

**Verification:**
- `isinstance(GreenhouseUnavailable(), IngestionUnavailable)` and `isinstance(GreenhouseParseError(), IngestionParseError)` both hold; full existing ingestion test suite still passes.

---

### U2. Add Lever and Ashby to the `ats` choices

**Goal:** Model-level support for the two new platforms.

**Requirements:** R6

**Dependencies:** None

**Files:**
- Modify: `apps/jobs/models.py`
- Create: new migration (auto-generated via `makemigrations`)

**Approach:**
- Add `LEVER = "lever", "Lever"` and `ASHBY = "ashby", "Ashby"` to `JobSource.ATS`. `DiscoveredBoard.ats` reuses `JobSource.ATS.choices`, so no separate change needed there.

**Test scenarios:**
- Test expectation: none — pure schema/choices addition, no new behavior to assert beyond the migration applying cleanly.

**Verification:**
- Migration applies cleanly against an existing database; `JobSourceAdmin`/`DiscoveredBoardAdmin` `list_filter` on `ats` shows the new choices without further code changes (confirmed generic in research).

---

### U3. `LeverClient` and Lever normalizer

**Goal:** Fetch and normalize jobs from Lever's public Postings API.

**Requirements:** R4, R5

**Dependencies:** U1, U2

**Files:**
- Create: `apps/jobs/ingestion/lever_client.py`
- Modify: `apps/jobs/ingestion/exceptions.py` (add `LeverError`/`LeverUnavailable`/`LeverParseError` following U1's base-class pattern)
- Modify: `apps/jobs/ingestion/normalizers.py` (add `normalize_lever_job`; rename existing `normalize_job` to `normalize_greenhouse_job` for symmetry with the other per-platform functions, updating the one call site in `greenhouse_client.py`)
- Test: `apps/jobs/tests/test_lever_client.py`
- Test: fixture `apps/jobs/tests/fixtures/lever_board.json`

**Approach:**
- Mirror `GreenhouseClient`'s constructor and `_get_with_retry`/`_parse_body` internals exactly (same retryable-status set, same backoff and `Retry-After` handling).
- `fetch_jobs(board_token)` calls Lever's Postings API (`https://api.lever.co/v0/postings/{board_token}?mode=json`) and returns normalized dicts via `normalize_lever_job`.
- `normalize_lever_job` produces the exact same dict shape as `normalize_greenhouse_job`; routes the raw location string through `apps.locations.normalize_location` per the institutional learning on location matching, not a bespoke substring parser.

**Patterns to follow:**
- `apps/jobs/ingestion/greenhouse_client.py` (client), `apps/jobs/ingestion/normalizers.py::normalize_job` (normalizer contract), `apps/jobs/tests/test_greenhouse_client.py` (test structure).

**Execution note:** Fetch one real board's response from Lever's Postings API and confirm actual field names before writing `normalize_lever_job` and its fixtures — the response shape named in Approach is directional (see Open Questions), and fixtures built against guessed field names would pass regardless of whether the guess is right.

**Test scenarios:**
- Happy path: valid postings payload → correct list of normalized dicts, matching the shared normalized shape.
- Edge case: malformed JSON response → `LeverParseError`.
- Edge case: response missing the expected list field → `LeverParseError`.
- Error path: transient 5xx then recovery → succeeds after retry.
- Error path: retries exhausted on persistent 5xx → `LeverUnavailable`.
- Error path: 429 with `Retry-After` header → delay honored before retry.
- Error path: non-retryable 4xx → fails immediately, no retry.
- Error path: network timeout → `LeverUnavailable`.
- Integration: a job with an ambiguous location string resolves through `apps.locations.normalize_location` rather than a Lever-specific substring match.

**Verification:**
- `isinstance(LeverUnavailable(), IngestionUnavailable)` holds; test suite covers the same scenario categories as `test_greenhouse_client.py`.

---

### U4. `AshbyClient` and Ashby normalizer

**Goal:** Fetch and normalize jobs from Ashby's public Job Board API.

**Requirements:** R4, R5

**Dependencies:** U1, U2

**Files:**
- Create: `apps/jobs/ingestion/ashby_client.py`
- Modify: `apps/jobs/ingestion/exceptions.py` (add `AshbyError`/`AshbyUnavailable`/`AshbyParseError`)
- Modify: `apps/jobs/ingestion/normalizers.py` (add `normalize_ashby_job`)
- Test: `apps/jobs/tests/test_ashby_client.py`
- Test: fixture `apps/jobs/tests/fixtures/ashby_board.json`

**Approach:**
- Same client/normalizer shape as U3, targeting Ashby's Job Board API (`https://api.ashbyhq.com/posting-api/job-board/{board_token}`).
- `normalize_ashby_job` routes location through `apps.locations.normalize_location`, same as Lever.

**Patterns to follow:**
- Same as U3.

**Execution note:** Same as U3 — fetch one real board's response from Ashby's Job Board API and confirm actual field names before writing `normalize_ashby_job` and its fixtures.

**Test scenarios:**
- Same category set as U3 (happy path, malformed JSON, missing field, transient recovery, exhausted retries, `Retry-After`, non-retryable 4xx, network timeout, location-normalization integration), adapted to Ashby's response shape.

**Verification:**
- Same as U3.

---

### U5. ATS client dispatch registry

**Goal:** Central mapping from `ats` value to client class, replacing hardcoded `GreenhouseClient()` instantiation.

**Requirements:** R1, R2, R3

**Dependencies:** U2, U3, U4

**Files:**
- Create: `apps/jobs/ingestion/dispatch.py`
- Test: `apps/jobs/tests/test_dispatch.py`

**Approach:**
- A dict `CLIENT_REGISTRY = {JobSource.ATS.GREENHOUSE: GreenhouseClient, JobSource.ATS.LEVER: LeverClient, JobSource.ATS.ASHBY: AshbyClient}` plus a `get_client(ats: str)` helper that raises a clear error for an unregistered ats value.

**Test scenarios:**
- Happy path: `get_client(ATS.GREENHOUSE)` returns a `GreenhouseClient` instance (and likewise for Lever/Ashby).
- Error path: `get_client()` with an unregistered ats value raises a clear, typed error.

**Verification:**
- Registry covers all three currently-supported platforms; adding a platform later requires only a one-line registry addition (per R3's forward-looking intent).

---

### U6. Wire dispatch into `ingest_source`, `register_job_source`, and `add_job_source`

**Goal:** Replace every remaining hardcoded `GreenhouseClient()` instantiation with the dispatch registry.

**Requirements:** R1, R3

**Dependencies:** U1, U5

**Files:**
- Modify: `apps/jobs/tasks.py` (`ingest_source`)
- Modify: `apps/jobs/ingestion/register.py` (`register_job_source` gains an `ats` parameter, default `JobSource.ATS.GREENHOUSE`; uses `get_client(ats)` instead of hardcoding)
- Modify: `apps/jobs/management/commands/add_job_source.py` (add `--ats` flag, default `greenhouse`, passed through to `register_job_source`)
- Modify: `apps/jobs/admin.py` (`DiscoveredBoardAdmin.approve` passes `board.ats` to `register_job_source` and catches `(IngestionUnavailable, IngestionParseError)` instead of the Greenhouse-specific classes)
- Test: `apps/jobs/tests/test_register_job_source.py` (extend for `ats` parameter)
- Test: `apps/jobs/tests/test_admin_discovered_board.py` (extend for non-Greenhouse approval)

**Approach:**
- `ingest_source` looks up `get_client(source.ats)` instead of `GreenhouseClient()`; everything downstream of `fetch_jobs` is unchanged (confirmed ATS-agnostic in research).
- `register_job_source` and the management command default to `GREENHOUSE` so any other untouched caller keeps working, while new call sites pass the real ats explicitly.
- `register_job_source` changes `Employer.objects.get_or_create(slug=token, ...)` to derive the slug from `slugify(name)` instead (see Key Technical Decisions) — required before this unit can safely register non-Greenhouse sources, since a raw `board_token` is not always slug-safe.

**Test scenarios:**
- Covers AE1. Happy path: `JobSource` with `ats=ATS.LEVER` → `ingest_source` fetches via `LeverClient`, jobs land in the DB through the same upsert path as Greenhouse.
- Happy path: `add_job_source --ats lever <token>` registers a Lever `JobSource`.
- Happy path: `DiscoveredBoardAdmin.approve` on an Ashby candidate creates an Ashby `JobSource` via `register_job_source(ats=ATS.ASHBY, ...)`.
- Happy path: `register_job_source` creates an `Employer` whose `slug` is derived from the employer name, not the raw token.
- Edge case: two different platforms registering boards with the same short token (e.g. two companies both using `careers` as their board token on different ATSs) resolve to two distinct `Employer` rows, not one.
- Error path: approving a candidate whose live re-fetch fails raises/handles via the generic `IngestionUnavailable`/`IngestionParseError` catch, regardless of which platform it is.
- Regression: existing Greenhouse-only tests in `test_register_job_source.py` and `test_admin_discovered_board.py` continue passing with the default `ats` value.

**Verification:**
- No remaining direct `GreenhouseClient()` instantiation outside the dispatch registry itself and its tests.

---

### U7. Generalize discovery for Lever and Ashby

**Goal:** `discover_boards` finds and validates Lever and Ashby candidates the same way it does Greenhouse ones.

**Requirements:** R2, R7, R8

**Dependencies:** U1, U5, U6

**Files:**
- Modify: `apps/jobs/ingestion/board_search.py` (`search_boards(ats)` replacing `search_greenhouse_boards()`, mapping `ats` → dataset CSV filename `{greenhouse,lever,ashby}.csv`)
- Modify: `apps/jobs/tasks.py` (`discover_boards` loops over the platforms currently registered for discovery instead of hardcoding Greenhouse; uses `get_client(ats)` for validation and the generic exception base classes for the per-token try/except)
- Test: `apps/jobs/tests/test_board_search.py` (extend for `search_boards(ats)`, cover unsupported ats)
- Test: `apps/jobs/tests/test_discover_boards.py` (extend for Lever/Ashby candidate discovery)
- Test: fixtures `apps/jobs/tests/fixtures/lever_companies.csv`, `apps/jobs/tests/fixtures/ashby_companies.csv`

**Approach:**
- `known_tokens` filtering (already-active `JobSource`, already-pending `DiscoveredBoard`) becomes per-ats rather than hardcoded to `ats=JobSource.ATS.GREENHOUSE`.
- Per-source failure isolation (one bad token/platform doesn't abort the run) is preserved — mirrors the existing Greenhouse behavior research confirmed in `test_discover_boards.py`.

**Test scenarios:**
- Covers AE2. Happy path: a new, valid Lever candidate token creates a pending `DiscoveredBoard` with `ats=ATS.LEVER`; it is not ingested by the hourly sweep until approved.
- Happy path: a new, valid Ashby candidate token is discovered and queued the same way.
- Edge case: a token already known as an active `JobSource` or already-pending `DiscoveredBoard` for its ats is skipped without a redundant validation call.
- Error path: an unreachable/malformed Lever or Ashby CSV dataset source completes the run reporting zero found for that platform rather than aborting the whole run.
- Regression: existing Greenhouse discovery behavior (cap enforcement, per-run isolation) is unchanged.

**Verification:**
- Discovery runs surface results per-platform in logs/metrics as before, for all three platforms.

---

### U8. Vendor `WorkdayScraper` and add the Workday normalizer adapter

**Goal:** Fetch Workday jobs without reimplementing pagination/facet-subdivision logic in jobborg.

**Requirements:** R9, R10, R11

**Dependencies:** U1

**Files:**
- Create: `apps/jobs/ingestion/vendor/workday/scraper.py` — copied and adapted from jobhive's `src/jobhive/scrapers/workday.py`
- Create: `apps/jobs/ingestion/vendor/workday/base.py` — trimmed copy of jobhive's `src/jobhive/scrapers/base.py`, `BaseScraper` only (no `ScraperRegistry` — this plan's own `dispatch.py` replaces it)
- Create: `apps/jobs/ingestion/vendor/workday/models.py` — trimmed copy of jobhive's `src/jobhive/models.py`, `Job`/`Salary`/`EmploymentType`/`SalaryPeriod` only
- Create: `apps/jobs/ingestion/vendor/workday/exceptions.py` — trimmed copy of jobhive's `src/jobhive/exceptions.py`, `JobHiveError`/`ScraperError`/`CompanyNotFoundError` only
- Modify: `apps/jobs/models.py` (add `WORKDAY = "workday", "Workday"` to `JobSource.ATS`)
- Create: new migration
- Modify: `apps/jobs/ingestion/exceptions.py` (add `WorkdayError`/`WorkdayUnavailable`/`WorkdayParseError`)
- Modify: `apps/jobs/ingestion/normalizers.py` (add `normalize_workday_job` mapping the vendored scraper's job output to the shared dict shape)
- Modify: `requirements/base.txt` (add `httpx` and `pydantic`, pinned)
- Test: `apps/jobs/tests/test_workday_client.py`
- Test: fixture(s) representative of Workday's `cxs` API response shape

**Approach:**
- Vendor the confirmed minimal file set above (see Open Questions — Resolved During Planning), rewriting `workday.py`'s `from jobhive.X import Y` statements to point at the vendored modules instead of the `jobhive` package. This is what avoids the `pandas` dependency: none of these four files import `jobhive.client`, only `jobhive/__init__.py` does.
- Attribute the source in a header comment on `scraper.py`, pinned to the exact upstream commit SHA vendored from (not just "a version") — MIT-licensed, pointing at the origin repo (see the Workday-drift risk in Risks & Dependencies).
- Before writing `normalize_workday_job`, confirm the pydantic major version jobhive's vendored `models.py` was written against (check jobhive's own `pyproject.toml`) and reconcile it with whatever `pydantic` version this unit pins in `requirements/base.txt` — a mismatch fails at import time or silently misvalidates fields.
- `WorkdayClient.fetch_jobs(board_token)` validates `board_token` against the `https://{company}.{instance}.myworkdayjobs.com/{site}` hostname pattern (same pattern the vendored scraper parses `company`/`instance`/`site` from) and rejects anything that doesn't match, before issuing any request — this is the SSRF guard from Key Technical Decisions, and it covers both `discover_boards`'s pre-review validation fetch and the admin approval re-fetch, since both go through this one client.
- `WorkdayClient.fetch_jobs` bridges the vendored scraper's async/httpx interface into the synchronous interface every other client exposes (e.g. `asyncio.run(scraper.fetch(...))` per call, with the underlying `httpx.AsyncClient`'s lifecycle scoped to that single call) — every caller (`ingest_source`, `register_job_source`, the dispatch registry) is synchronous today, so this bridging is required, not optional.
- `normalize_workday_job` maps the vendored scraper's `Job` output to the same dict shape as `normalize_greenhouse_job`/`normalize_lever_job`/`normalize_ashby_job`, including routing location through `apps.locations.normalize_location`.

**Execution note:** Since this unit depends on code not yet copied into the repo, start by vendoring the scraper source and getting one real board's response fetched and printed before writing `normalize_workday_job` against real field names — the exact response shape should be confirmed against live data rather than assumed from the origin research notes.

**Patterns to follow:**
- `apps/jobs/ingestion/greenhouse_client.py`'s `fetch_jobs` interface, for the `WorkdayClient` wrapper shape.

**Test scenarios:**
- Covers AE3. Happy path: a Workday `JobSource` → `WorkdayClient.fetch_jobs` returns normalized dicts matching the shared shape, with no Workday-specific fields required by `upsert_jobs`.
- Edge case: a tenant whose job count requires facet subdivision (i.e., exercises the vendored pagination-cap workaround) still returns the full job set.
- Error path: an unreachable Workday tenant → `WorkdayUnavailable`.
- Error path: an unparseable response → `WorkdayParseError`.
- Security: a `board_token` that doesn't match the Workday hostname pattern (e.g. an arbitrary internal or non-Workday URL) is rejected by `WorkdayClient` before any request is made.

**Verification:**
- `isinstance(WorkdayUnavailable(), IngestionUnavailable)` holds; `pip list` / `requirements/base.txt` shows `httpx` and `pydantic` added but no `pandas`; the pinned `pydantic` version matches what the vendored models were written against.

---

### U9. Wire Workday into dispatch and discovery

**Goal:** Workday participates in ingestion and discovery the same way Lever/Ashby do.

**Requirements:** R12

**Dependencies:** U7, U8

**Files:**
- Modify: `apps/jobs/ingestion/dispatch.py` (register `WorkdayClient`)
- Modify: `apps/jobs/ingestion/board_search.py` (`search_boards` supports `ats=workday`, mapping to `workday.csv`)
- Modify: `apps/jobs/tasks.py` (`discover_boards` includes Workday in its per-platform loop)
- Test: `apps/jobs/tests/test_dispatch.py` (extend)
- Test: `apps/jobs/tests/test_discover_boards.py` (extend for Workday candidate discovery)

**Approach:**
- Same shape as U5/U7, now with a third registry entry.

**Test scenarios:**
- Happy path: a new Workday candidate is discovered, queued as a pending `DiscoveredBoard`, and — once approved — ingested on the next sweep via the dispatch registry.
- Regression: Greenhouse/Lever/Ashby discovery and ingestion are unaffected by Workday's addition.

**Verification:**
- All four platforms (`greenhouse`, `lever`, `ashby`, `workday`) are present in the dispatch registry and in `discover_boards`'s per-platform loop.

---

## System-Wide Impact

- **Interaction graph:** `ingest_source`, `discover_boards`, `register_job_source`, the `add_job_source` management command, and `DiscoveredBoardAdmin.approve` are all touched. `JobSourceAdmin`/`DiscoveredBoardAdmin` `list_filter` on `ats` and `JobAdmin`'s `source_ats` filter adapt automatically — confirmed generic in research, no code change needed there.
- **Error propagation:** Dispatch and admin code catch the shared `IngestionUnavailable`/`IngestionParseError` base classes (U1) instead of per-ATS exception tuples, so a new platform's exceptions don't require touching those call sites again.
- **State lifecycle risks:** None new — `upsert_jobs`, `content_hash` computation, and classification enqueueing are unchanged and already ATS-agnostic (confirmed in research).
- **API surface parity:** The `add_job_source` CLI gains an `--ats` flag for parity with the admin-based discovery/approval flow, which already threads `ats` through once U6 lands.
- **Integration coverage:** End-to-end discovery→review→ingest scenarios per new platform (candidate found → validated → `DiscoveredBoard` created → reviewer approves → `JobSource` created → next `ingest_source` sweep picks it up), mirroring the existing Greenhouse coverage in `test_discover_boards.py` and `test_admin_discovered_board.py`.
- **Unchanged invariants:** `upsert_jobs`, the `Job` model, and content-hash-based closure detection are explicitly not modified by this plan.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Lever/Ashby public API response shape drifts from what's assumed at plan time | Confirm exact field names against a live fetch before finalizing each normalizer (U3/U4 execution notes); covered by fixture-based tests that fail loudly on shape mismatch. |
| Workday's `board_token` is a server-fetched URL rather than a slug, making an unvalidated token a server-side-request-forgery surface reachable by the unattended discovery sweep before any human review | `WorkdayClient` rejects any token not matching the Workday hostname pattern before issuing a request (U8) — see Key Technical Decisions. |
| Vendored Workday scraper (including a pydantic model) drifts from upstream jobhive fixes over time, with no package-manager-level integrity check on hand-copied code | Pin to an exact upstream commit SHA in the vendored file's header comment (not just "a version"); treat the vendor as requiring the same code-review scrutiny as first-party code, not just a functional test pass. |
| New `httpx`/`pydantic` runtime dependencies introduce version-compatibility risk with the existing Django/psycopg/celery stack, and the vendored Workday models may target a different pydantic major version than what's pinned | Pin exact versions in `requirements/base.txt`; confirm the pydantic version jobhive's vendored models were written against before pinning (U8); run the full existing test suite before merging U8. |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-07-21-ats-platform-expansion-requirements.md](docs/brainstorms/2026-07-21-ats-platform-expansion-requirements.md)
- Related code: `apps/jobs/ingestion/greenhouse_client.py`, `apps/jobs/ingestion/normalizers.py`, `apps/jobs/ingestion/exceptions.py`, `apps/jobs/ingestion/upsert.py`, `apps/jobs/ingestion/register.py`, `apps/jobs/ingestion/board_search.py`, `apps/jobs/tasks.py`, `apps/jobs/admin.py`, `apps/jobs/management/commands/add_job_source.py`
- Related issues: #18, #22, #23
- External: `github.com/kalil0321/ats-scrapers` (jobhive-py), user's fork `yash10019coder/ats-scrapers`
