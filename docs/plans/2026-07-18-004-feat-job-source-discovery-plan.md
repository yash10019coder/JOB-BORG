---
title: Automated Job Source Discovery
type: feat
status: completed
date: 2026-07-18
origin: docs/brainstorms/2026-07-18-job-source-discovery-requirements.md
---

# Automated Job Source Discovery

## Summary

Add a `DiscoveredBoard` model and a daily Celery task that queries Bing for `boards.greenhouse.io` URLs, extracts candidate board tokens, validates each against the live Greenhouse API (reusing `GreenhouseClient.fetch_jobs`), and stores validated candidates as pending review rows. A new Django admin `approve`/`reject` action on `DiscoveredBoard` promotes an approved candidate into a `JobSource` through a shared registration helper extracted from `add_job_source`, so the manual and automated paths create employers and sources identically.

---

## Problem Frame

Coverage is stagnant because `JobSource` registration is entirely manual — `add_job_source` requires someone to already know and type a company's Greenhouse token. This plan builds the missing "find new companies" half of the pipeline; the hourly ingestion sweep and match scoring downstream are unchanged (see origin: docs/brainstorms/2026-07-18-job-source-discovery-requirements.md).

---

## Requirements

- R1. A scheduled job runs daily and searches for Greenhouse board URLs, independent of and in addition to the existing hourly ingestion sweep.
- R2. The job extracts candidate Greenhouse board tokens from the search results.
- R3. Candidate tokens that already have an active `JobSource`, or are already pending review, are skipped without a redundant validation call.
- R4. Each remaining new candidate token is validated against the live Greenhouse API, reusing the same validation `add_job_source` already performs.
- R5. Tokens that fail validation are discarded — they are never queued for review.
- R6. A single failed search query or a single failed token validation does not abort the rest of that day's run.
- R7. Tokens that pass validation are stored as pending discovered boards, visible to a reviewer, until explicitly approved or rejected.
- R8. A reviewer can approve a pending discovered board; approval creates a `JobSource` (and `Employer` if one doesn't exist) exactly as `add_job_source` does today.
- R9. A reviewer can reject a pending discovered board; rejected tokens are not re-suggested by future discovery runs. Re-adding a rejected token later goes through the existing manual `add_job_source` path.
- R10. No discovered board becomes an active `JobSource` without an explicit reviewer approval.
- R11. Each discovery run's results are logged and visible on the existing Grafana/Loki stack, so a stall is visible rather than failing silently.

**Origin actors:** A1 (Discovery pipeline / system), A2 (Reviewer / staff-admin), A3 (Ingestion sweep / existing system, unchanged)
**Origin flows:** F1 (Daily discovery run), F2 (Reviewer approves or rejects a discovered board)
**Origin acceptance examples:** AE1 (covers R3), AE2 (covers R4, R5), AE3 (covers R6, R11), AE4 (covers R8, R10)

---

## Scope Boundaries

- Other ATS platforms (Lever, Workday, Ashby, etc.) — Greenhouse only.
- Auto-approval of discovered boards — every board requires explicit reviewer action.
- A fixed numeric coverage target — not measured by this plan.
- Paid/official search APIs — rejected; direct scraping only.
- Any review UI beyond Django admin — no custom frontend screen.
- Self-serve company/board submission by end users.
- Automated content moderation of discovered-board job postings — trust is placed in the reviewer's judgment at approval time, not in automated scanning (see origin Deferred / Open Questions: "Discovered-board content is never treated as untrusted external input"). This plan does not build a content classifier; it is limited to the identity-signal mitigation in U4.

---

## Context & Research

### Relevant Code and Patterns

- `apps/jobs/management/commands/add_job_source.py` — the exact validate-then-persist sequence (`GreenhouseClient.fetch_jobs` → dedupe check → `Employer.objects.get_or_create` → `JobSource.objects.create`) that U1 extracts into a shared helper.
- `apps/jobs/ingestion/greenhouse_client.py` — `GreenhouseClient.fetch_jobs(board_token)` is a standalone, DB-free method with built-in retry/backoff (`max_retries=3`, exponential backoff, honors `Retry-After`) and raises `GreenhouseUnavailable` / `GreenhouseParseError` (`apps/jobs/ingestion/exceptions.py`). Reusable as-is for validation.
- `apps/jobs/tasks.py` — `ingest_all_active_sources` is the per-source failure-isolation template (`try`/`except` + `logger.exception`, returns per-item stat dicts) that U3's discovery task mirrors. Task naming convention: `@shared_task(name="apps.jobs.<verb_noun>")`.
- `config/settings/base.py` — `CELERY_BEAT_SCHEDULE` (static, `crontab`-based) is where the existing hourly sweep is scheduled; U3 adds a sibling daily entry.
- `apps/jobs/models.py` — `JobSource`/`Job` model conventions to mirror in `DiscoveredBoard`: `TextChoices` inner class for status/enum fields, `is_`/`needs_`-prefixed booleans, explicit `on_delete` + `related_name` on FKs, `created_at`/`updated_at` auto timestamp pair, `models.UniqueConstraint` (not `unique_together`) in `Meta.constraints`.
- `apps/jobs/admin.py` — currently plain `ModelAdmin` subclasses with no custom actions; U4's approve/reject actions are the first custom admin actions in this codebase, so they use Django's standard `@admin.action` decorator with no competing local convention to reconcile.
- `apps/jobs/tests/test_add_job_source_command.py`, `test_greenhouse_client.py` — test conventions: plain `django.test.TestCase`, `responses` library for HTTP mocking (`@responses.activate`), JSON fixtures under a `tests/fixtures/` directory, no factory library.

### Institutional Learnings

- `docs/solutions/` does not exist in this repository yet — no institutional learnings to draw from. Once this feature lands, capturing the `DiscoveredBoard` design and the shared-registration-helper extraction via `/ce-compound` would seed that knowledge base for future ingestion work.

### External References

- Search-engine scraping feasibility research (2026 guides — ScrapFly, Olostep, ApiSerpent): plain `requests`-based scraping against Google fails almost immediately due to JS-fingerprinting bot defenses, independent of parsing quality; Bing is meaningfully more tolerant of plain-HTTP scraping at low query volume and is the realistic target for this plan.
- Directory/technographic datasets that enumerate companies using Greenhouse (Apify's Greenhouse Companies dataset, TheirStack.com's Greenhouse technology list) exist as a lower-risk fallback data source if scraping proves unstable in practice — noted as a risk mitigation, not adopted now (see Risks & Dependencies).

---

## Key Technical Decisions

- **Scrape Bing, not Google:** research shows plain-HTTP scraping against Google's SERP is blocked almost immediately regardless of resilience patterns (JS-fingerprinting bot defenses, not a parsing problem); Bing tolerates low-volume plain-HTTP scraping and is the only realistic target given the existing codebase's plain `requests` client and the brainstorm's explicit rejection of a paid search API or a headless-browser dependency.
- **Broad `site:boards.greenhouse.io` query, not a seeded company-name list:** the pipeline's purpose is finding companies not already known to the team; a name-seeded query set would only re-confirm candidates already considered. Query volume is capped per run (see U2) to manage block risk.
- **New `DiscoveredBoard` model, not a status flag on `JobSource`:** `JobSource` has no unapproved/pending state today and its lifecycle (`is_active` sweep membership) is a different concern from review-queue membership; a separate model keeps the ingestion sweep's `JobSource.objects.filter(is_active=True)` query untouched and avoids adding review-only fields to a model the hourly sweep reads on every run.
- **Extract a shared `register_job_source` helper used by both `add_job_source` and the admin approve action:** avoids a third, drifting copy of the validate-then-persist sequence; `add_job_source.py` is refactored to call it (low risk — the sequence is already covered by `test_add_job_source_command.py`).
- **No custom Prometheus metric for R11 — structured logging only:** no custom-metric precedent exists in `apps/jobs` today (task-level throughput/failure/runtime already comes free from `celery-exporter`, per `docker-compose.yml`'s `-E` flag and `CELERY_WORKER_SEND_TASK_EVENTS`). The discovery task emits the same `logger.info("...", stats)` / `logger.exception(...)` shape as `ingest_all_active_sources`, which Loki already ingests and Grafana can query — consistent with the existing observability stack rather than introducing a new metrics path.
- **Lightweight identity-signal mitigation for spoofed/squatted boards:** the admin detail view flags existing `Employer`s with a similar name (case-insensitive substring match) as a signal for the reviewer, addressing the origin doc's deferred "no way to catch a spoofed board" concern without building a full identity-verification system (see origin Deferred / Open Questions).

---

## Open Questions

### Resolved During Planning

- Reviewer surface (Django admin vs. dedicated screen): Django admin — matches the origin's "no dedicated review UI beyond minimum" scope boundary, and no competing admin-action convention exists to displace.
- Scraping mechanism (headless browser vs. plain HTTP client): plain `requests` against Bing — see Key Technical Decisions.
- Query pattern (broad enumeration vs. targeted company-name list): broad `site:boards.greenhouse.io` search, capped volume per run.

### Deferred to Implementation

- Exact query volume cap and backoff/circuit-breaker thresholds before the daily task self-suppresses further queries for the run — informed by observed Bing block behavior in practice, not knowable at plan time.
- Whether Bing's result markup requires any HTML-parsing library beyond what's already available, or a minimal regex/string extraction over the response is sufficient for the narrow `boards.greenhouse.io/<token>` URL pattern — depends on actually inspecting a live response.
- Whether the existing-`Employer`-name-similarity check (U4) needs anything beyond a simple case-insensitive substring match, or produces too many false positives in practice once real discovered boards start flowing through — revisit after the first review sessions if the signal proves noisy.

---

## Implementation Units

### U1. `DiscoveredBoard` model and shared registration helper

**Goal:** Add the review-queue data model and extract the validate-then-persist sequence out of `add_job_source` so both the manual command and the future admin approve action share one implementation.

**Requirements:** R3, R7, R8, R9, R10

**Dependencies:** None

**Files:**
- Create: `apps/jobs/models.py` (add `DiscoveredBoard` model — modify existing file)
- Create: `apps/jobs/migrations/000X_discoveredboard.py`
- Create: `apps/jobs/ingestion/register.py` (new `register_job_source(token, employer_name=None)` helper)
- Modify: `apps/jobs/management/commands/add_job_source.py` (call the extracted helper instead of inlining the sequence)
- Modify: `apps/jobs/admin.py` (register `DiscoveredBoard` with a basic `ModelAdmin` — approve/reject actions land in U4)
- Test: `apps/jobs/tests/test_models.py`
- Test: `apps/jobs/tests/test_register_job_source.py`
- Test: `apps/jobs/tests/test_add_job_source_command.py` (extend existing coverage to confirm behavior is unchanged after the refactor)

**Approach:**
- `DiscoveredBoard` fields: `ats` (mirrors `JobSource.ATS`), `board_token`, `source_url` (where it was found), `derived_employer_name`, `status` (`TextChoices`: `pending`, `approved`, `rejected`), `discovered_at` (`auto_now_add`), `reviewed_at` (nullable), `created_at`/`updated_at`. `UniqueConstraint` on `(ats, board_token)` so a token can only have one `DiscoveredBoard` row at a time, which is what R3's dedup and R9's non-resurfacing both key off.
- `register_job_source(token, employer_name=None)` in `apps/jobs/ingestion/register.py` contains exactly the sequence currently inlined in `add_job_source._register_one`: fetch via `GreenhouseClient`, catch `(GreenhouseUnavailable, GreenhouseParseError)`, check for an existing `JobSource`, `Employer.objects.get_or_create`, `JobSource.objects.create`. Returns enough info (created employer/source, job count) for both the command's stdout messages and the future admin action's confirmation message.
- Refactor `add_job_source.py` to call `register_job_source` and keep its existing stdout/stderr messaging — this is a behavior-preserving refactor, not a new feature.

**Patterns to follow:**
- `apps/jobs/models.py` `JobSource`/`Job` — `TextChoices`, FK `on_delete`/`related_name`, `Meta.constraints` with named `UniqueConstraint`.
- `apps/jobs/migrations/0002_job_source_url.py` (or nearest small, intent-named migration) for the new migration's naming style.

**Test scenarios:**
- Happy path: `register_job_source("stripe")` with a valid live-fetch response creates one `Employer` and one `JobSource`, returns success info.
- Happy path: `register_job_source("stripe", employer_name="Stripe Inc")` uses the override name for `Employer` creation.
- Edge case: `register_job_source` called for a token with an existing `JobSource` returns an "already registered" result without creating a duplicate.
- Error path: `GreenhouseUnavailable`/`GreenhouseParseError` raised by the client propagates (or is translated) so both the command and the admin action can surface a clear failure message.
- `add_job_source` command: existing test suite in `test_add_job_source_command.py` still passes unmodified against the refactored implementation (regression check).
- `DiscoveredBoard` model: `UniqueConstraint(ats, board_token)` rejects a second pending row for the same token (`IntegrityError` on duplicate create).

**Verification:**
- Migration applies cleanly; `DiscoveredBoard` is queryable and enforces the token uniqueness constraint.
- `add_job_source` command behavior (stdout messages, DB side effects) is unchanged from before the refactor.

---

### U2. Bing board-search client

**Goal:** A standalone client that queries Bing for `boards.greenhouse.io` URLs and returns extracted candidate board tokens, isolated from the discovery task so it can be tested and reasoned about independently.

**Requirements:** R1, R2, R6

**Dependencies:** None

**Files:**
- Create: `apps/jobs/ingestion/board_search.py`
- Create: `apps/jobs/ingestion/exceptions.py` (extend with a `BoardSearchUnavailable` or similar exception — modify existing file)
- Test: `apps/jobs/tests/test_board_search.py`
- Test fixtures: `apps/jobs/tests/fixtures/bing_search_results.html` (or similar sample response)

**Approach:**
- Uses `requests` (already the repo's only HTTP client) to query Bing with a `site:boards.greenhouse.io` search, with a realistic non-default `User-Agent` header.
- Extracts candidate tokens from result URLs matching the `boards.greenhouse.io/<token>` pattern; returns a de-duplicated list of raw token strings for the caller (U3) to filter against known `JobSource`/`DiscoveredBoard` rows.
- Caps query volume per invocation (small, fixed number of result pages) rather than open-ended pagination, to bound both block risk and the exact volume ceiling deferred to implementation.
- Resilience: exponential backoff with jitter on non-2xx responses; a simple failure counter that stops the run early (rather than retry-storming) if repeated requests fail, surfaced to the caller as a partial-results-with-failure-flag return rather than a raised exception, so R6's per-query failure isolation holds without every call site needing its own try/except.
- Treats `robots.txt` awareness as a documented constraint on which paths this client queries, not runtime-checked code — Bing's `/search` tolerance for simple clients was research-validated for this narrow use case.

**Technical design:** *(directional guidance, not implementation specification)*

```
search_greenhouse_boards() -> SearchResult:
    for page in range(MAX_PAGES_PER_RUN):
        response = get(bing_search_url, params={q: "site:boards.greenhouse.io", page})
        if not ok(response):
            record_failure(); if failure_count >= THRESHOLD: break
            continue
        tokens += extract_tokens(response.text)
    return SearchResult(tokens=dedupe(tokens), pages_fetched, failures)
```

**Patterns to follow:**
- `apps/jobs/ingestion/greenhouse_client.py` — retry/backoff shape and injectable `session`/`sleep` for testability; `apps/jobs/ingestion/exceptions.py` for the exception style to extend.

**Test scenarios:**
- Happy path: a mocked Bing response containing several `boards.greenhouse.io/<token>` URLs returns the correct de-duplicated token list.
- Edge case: a response with zero matching URLs returns an empty token list without error.
- Edge case: the same token appearing in multiple result URLs (different query params) is de-duplicated to one entry.
- Error path: a non-2xx response is retried per the backoff policy, then counted as a failure without raising; the run continues to the next page/query rather than aborting.
- Error path: failures exceeding the threshold stop further queries for that invocation and the partial token list plus a failure flag is returned.

**Verification:**
- Given a fixture Bing response, the client returns exactly the tokens embedded in that fixture.
- A run with injected repeated failures terminates early rather than looping indefinitely, and reports the failure in its return value.

---

### U3. `discover_boards` Celery task and beat schedule

**Goal:** Wire the search client and validation together into the daily scheduled task: search → extract → dedup against known tokens → validate → persist pending `DiscoveredBoard` rows → log run stats.

**Requirements:** R1, R3, R4, R5, R6, R11

**Dependencies:** U1, U2

**Files:**
- Modify: `apps/jobs/tasks.py` (add `discover_boards` task)
- Modify: `config/settings/base.py` (add `CELERY_BEAT_SCHEDULE` entry)
- Test: `apps/jobs/tests/test_discover_boards.py`

**Approach:**
- `@shared_task(name="apps.jobs.discover_boards")`, following the existing `apps.jobs.<verb_noun>` naming convention.
- Calls U2's search client, then filters candidate tokens against `JobSource.objects.filter(ats=GREENHOUSE, board_token__in=...)` and `DiscoveredBoard.objects.filter(...)` in a single query each to implement R3's dedup without a validation call per already-known token.
- For each remaining new token, calls `GreenhouseClient.fetch_jobs` for validation (R4); on success, creates a `DiscoveredBoard(status=pending)` row with a derived employer name (same title-casing convention as `add_job_source`'s default); on `GreenhouseUnavailable`/`GreenhouseParseError`, discards the token (R5) and continues (R6), following `ingest_all_active_sources`'s per-item `try`/`except` + `logger.exception` isolation pattern.
- Emits a single structured `logger.info` stats line at the end of the run (boards found, validated, already-known, failed) — mirrors `ingest_source`'s `logger.info("Ingested source %s: %s", source_id, stats)` shape, giving R11 visibility through the existing Loki/Grafana stack without a new metrics path.
- Beat schedule entry added next to the existing `ingest-all-sources-hourly` entry in `CELERY_BEAT_SCHEDULE`, using `crontab` for a daily off-peak time.

**Patterns to follow:**
- `apps/jobs/tasks.py` `ingest_all_active_sources` — per-item failure isolation, returns a stats dict, structured logging shape.
- `config/settings/base.py` `CELERY_BEAT_SCHEDULE` — existing entry format for the sibling daily schedule.

**Test scenarios:**
- Covers AE1. Happy path: a token search result matching an existing active `JobSource` is skipped — no validation call made, no duplicate `DiscoveredBoard` created.
- Covers AE2. Happy path: a new token that validates successfully creates exactly one `pending` `DiscoveredBoard` row with the expected derived employer name.
- Covers AE2. Edge case: a new token that fails Greenhouse validation is discarded — no `DiscoveredBoard` row created, run continues.
- Covers AE3. Error path: the search client returns zero results (simulating a blocked/failed search step) — the task completes, logs a stats line reporting zero boards found, and does not raise.
- Edge case: a token already present as a `pending` `DiscoveredBoard` from a prior run is skipped without a new validation call (R3, second dedup branch).
- Integration: a single run containing one already-known token, one newly-valid token, and one invalid token produces exactly one new `DiscoveredBoard` row and a stats log reflecting all three outcomes correctly.

**Verification:**
- Running the task against a mix of known/new/invalid candidate tokens produces the expected `DiscoveredBoard` rows and no duplicates.
- The beat schedule entry is present and does not collide with or alter the existing hourly ingestion schedule.

---

### U4. Admin approve/reject actions for `DiscoveredBoard`

**Goal:** Let a reviewer act on pending discovered boards from Django admin — approve promotes to `JobSource` via the shared helper, reject excludes the token from future re-suggestion.

**Requirements:** R7, R8, R9, R10

**Dependencies:** U1

**Files:**
- Modify: `apps/jobs/admin.py` (extend the `DiscoveredBoard` admin registration from U1 with `list_display`, `list_filter`, and `approve`/`reject` `@admin.action`-decorated methods)
- Test: `apps/jobs/tests/test_admin_discovered_board.py`

**Approach:**
- `list_display` surfaces `board_token`, `derived_employer_name`, `status`, `discovered_at`, and a computed "similar existing employer" hint (queries `Employer.objects.filter(name__icontains=<derived name fragment>)`, excluding exact matches already resolved by U1's dedup) so the reviewer has an identity-corroboration signal per the origin doc's spoofed-board concern.
- `approve` action (bulk-capable via Django admin's standard action mechanism): for each selected `pending` row, calls U1's `register_job_source(token, employer_name=derived_employer_name)`; on success sets `status=approved`, `reviewed_at=now()`; on failure (e.g., board went offline between discovery and review) leaves the row `pending` and surfaces an admin error message rather than silently failing.
- `reject` action: sets `status=rejected`, `reviewed_at=now()` for each selected `pending` row — no `JobSource`/`Employer` side effects. U3's dedup query already excludes non-`pending` `DiscoveredBoard` rows from re-suggestion, so rejection naturally satisfies R9 without additional logic.
- No custom frontend — plain Django admin `ModelAdmin` with `actions = ["approve", "reject"]`, consistent with the Scope Boundary against a dedicated review UI.

**Patterns to follow:**
- Django's standard `@admin.action(description=...)` decorator (first use in this codebase per repo research — no existing local convention to reconcile against).
- `apps/jobs/admin.py` existing `JobSourceAdmin`/`JobAdmin` for `list_display`/`list_filter` style.

**Test scenarios:**
- Covers AE4. Happy path: approving a `pending` `DiscoveredBoard` creates a `JobSource` (and `Employer` if new), sets `status=approved`, and the board is eligible for the next `ingest_all_active_sources` run.
- Covers AE4. Happy path: rejecting a `pending` `DiscoveredBoard` sets `status=rejected` and creates no `JobSource`.
- Edge case: bulk-selecting a mix of `pending` and already-`approved`/`rejected` rows and running `approve` only acts on the still-`pending` ones.
- Error path: approving a `DiscoveredBoard` whose board has since gone offline (Greenhouse re-fetch fails during `register_job_source`) leaves the row `pending` and surfaces an admin-visible error rather than creating a broken `JobSource`.
- Integration: the "similar existing employer" hint appears in the admin list/detail view when an `Employer` with a matching name substring already exists, and does not appear when no such `Employer` exists.

**Verification:**
- A reviewer can select a `pending` row in Django admin, approve it, and see the resulting `JobSource` immediately (matching what `add_job_source` would have created for the same token).
- A rejected token does not reappear in a subsequent `discover_boards` run.

---

## System-Wide Impact

- **Interaction graph:** `discover_boards` (new, U3) and `ingest_all_active_sources` (existing, unchanged) both run on Celery Beat but touch disjoint data: discovery only writes `DiscoveredBoard`; ingestion only reads `JobSource.is_active=True`. The two never race on the same row.
- **Error propagation:** Both U2 (search) and U3 (validation) failures are caught and logged per-item/per-run, never raised out of the Celery task — matches the existing `ingest_all_active_sources` isolation posture so one bad day of scraping never marks the task itself as failed in Celery's eyes.
- **State lifecycle risks:** The `UniqueConstraint(ats, board_token)` on `DiscoveredBoard` (U1) is the guard against duplicate pending rows if a run is retried or overlaps; approval failure (U4) leaves the row `pending` rather than silently losing the candidate.
- **Unchanged invariants:** `JobSource`, `Job`, the hourly ingestion sweep, and match scoring are not modified by this plan — `add_job_source` keeps working exactly as it does today (via the U1 refactor, behavior-preserving) as the manual fallback path referenced by R9.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Bing scraping breaks or gets blocked in practice (markup changes, IP reputation) | U2's failure-threshold circuit breaker plus U3's structured logging make a stall visible via Loki/Grafana (R11) rather than silent; origin doc's Key Decisions already names a fallback path (paid API or a third-party Greenhouse-company directory dataset) to revisit if this proves unstable. |
| `register_job_source` extraction regresses `add_job_source`'s existing behavior | U1's test scenarios explicitly re-run the existing `test_add_job_source_command.py` suite against the refactored implementation as a regression check before building on top of it. |
| Reviewer queue backs up faster than it's cleared (origin doc's deferred "reviewer throughput" concern) | Out of this plan's scope to solve structurally (origin doc left it as a documented open question); Django admin's list view with `status=pending` filter at least makes queue size visible at a glance so the team notices a backlog forming. |
| Scraping-vs-total-cost tradeoff (origin doc's deferred cost-framing concern) proves wrong once scraper maintenance burden is felt | Addressed by design, not code: U2 isolates the scraping client behind a narrow interface (`board_search.py`) so swapping to a paid API or a directory dataset later is a contained change, not a rewrite. |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-07-18-job-source-discovery-requirements.md](../brainstorms/2026-07-18-job-source-discovery-requirements.md)
- Related code: `apps/jobs/models.py`, `apps/jobs/tasks.py`, `apps/jobs/admin.py`, `apps/jobs/ingestion/`, `apps/jobs/management/commands/add_job_source.py`, `config/settings/base.py`
- External docs: Bing vs. Google plain-HTTP scraping feasibility (2026 scraping-tooling guides), Apify/TheirStack Greenhouse-company directory datasets (fallback data source reference)
