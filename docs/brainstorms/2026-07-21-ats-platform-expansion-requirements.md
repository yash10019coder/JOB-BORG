---
date: 2026-07-21
topic: ats-platform-expansion
---

# ATS Platform Expansion (Lever, Ashby, Workday)

## Summary

Extend job ingestion beyond Greenhouse to Lever and Ashby via bespoke thin clients, and to Workday via a vendored jobhive scraper, all routed through the existing discovery-and-review pipeline. `ingest_source` is refactored to dispatch by ATS instead of hardcoding Greenhouse. Lever, Ashby, and the dispatch refactor ship first; Workday follows as a fast-follow in the same tracking issue.

---

## Problem Frame

Only Greenhouse boards are ingested today. `JobSource.ats` and `DiscoveredBoard.ats` already model multiple ATS platforms via a choices enum, but only `ATS.GREENHOUSE` exists, and both `ingest_source` and `discover_boards` (`apps/jobs/tasks.py`) hardcode `GreenhouseClient`. Job coverage — and therefore recommendation quality — is capped at whatever a single ATS surfaces, even though Lever and Ashby have public JSON board APIs as simple as Greenhouse's, and Workday hosts a large share of enterprise job postings that jobborg currently cannot see at all. GitHub issue #18 tracks closing this gap for Lever, Workday, and Ashby specifically (LinkedIn and Indeed are a separate concern — see Scope Boundaries).

---

## Requirements

**ATS dispatch**
- R1. `ingest_source` selects the client implementation based on the `JobSource.ats` value instead of always instantiating `GreenhouseClient`.
- R2. `discover_boards` selects the discovery/validation client based on the ATS being discovered instead of always instantiating `GreenhouseClient`.
- R3. Adding a new ATS does not require changes to the upsert/classification logic downstream of client fetch + normalize — only a new client and a new normalizer mapping.

**Lever and Ashby ingestion**
- R4. A `LeverClient` and an `AshbyClient` fetch jobs from each platform's public per-company board API, following the retry/error-handling pattern already established in `apps/jobs/ingestion/greenhouse_client.py`.
- R5. Each platform's raw job payload is normalized into the same dict shape `normalize_job()` already produces for Greenhouse, so downstream upsert/classification code needs no ATS-specific branching.
- R6. `ATS.LEVER` and `ATS.ASHBY` are added to the `JobSource`/`DiscoveredBoard` `ats` choices.

**Lever and Ashby discovery**
- R7. `discover_boards` (or an equivalent per-ATS extension of it) discovers candidate Lever and Ashby boards using the same `kalil0321/ats-scrapers` CSV dataset mechanism `board_search.py` already uses for Greenhouse (`lever.csv`, `ashby.csv`).
- R8. Candidate Lever and Ashby boards go through the same `DiscoveredBoard` pending-review-and-approval flow Greenhouse candidates already go through — no board becomes an active `JobSource` without reviewer approval.

**Workday ingestion (fast-follow)**
- R9. Workday job fetching is implemented by vendoring jobhive's `WorkdayScraper` (from `kalil0321/ats-scrapers` / the user's fork) as a dependency, rather than reimplementing Workday's pagination-cap and facet-subdivision logic in jobborg.
- R10. An adapter maps jobhive's `Job` model output to the same normalized dict shape `normalize_job()` produces for other platforms, so Workday jobs flow through the existing upsert/classification pipeline unchanged.
- R11. `ATS.WORKDAY` is added to the `ats` choices.

**Workday discovery (fast-follow)**
- R12. `discover_boards` discovers candidate Workday tenants using the same CSV-dataset mechanism (`workday.csv`), and candidates go through the same `DiscoveredBoard` review flow as the other platforms.

---

## Acceptance Examples

- AE1. **Covers R1, R3.** Given a `JobSource` with `ats=ATS.LEVER`, when `ingest_source` runs for that source, it fetches jobs using `LeverClient`, not `GreenhouseClient`, and the resulting jobs are upserted through the same code path as Greenhouse jobs.
- AE2. **Covers R8.** Given a newly discovered Ashby board that passes validation, when it appears in the review queue, it is not ingested by the hourly sweep until a reviewer explicitly approves it.
- AE3. **Covers R9, R10.** Given a Workday `JobSource`, when `ingest_source` runs for it, jobs fetched via the vendored `WorkdayScraper` appear in the database in the same normalized shape as jobs from any other ATS, with no Workday-specific fields required by downstream code.

---

## Success Criteria

- Job coverage grows across Lever, Ashby, and (once shipped) Workday without any change to the recommendation, classification, or review-approval logic — only ingestion-layer code changes.
- A reviewer approving a discovered Lever, Ashby, or Workday board has the same experience as approving a Greenhouse board today.
- Adding a further ATS platform after this work lands requires writing one client/normalizer (or vendoring one scraper) and registering it in the dispatch map — not touching `ingest_source`, `discover_boards`, or upsert logic.

---

## Scope Boundaries

- LinkedIn and Indeed are explicitly out of scope for this work — tracked separately (see below), since neither exposes a simple per-company public board API the way Greenhouse/Lever/Ashby/Workday do.
- Hand-rolling Workday's own pagination and facet-subdivision logic is out of scope — deliberately vendoring jobhive's existing implementation instead.
- Any UI/UX changes to the discovered-board review flow are out of scope — the existing review mechanism is reused as-is.

---

## Key Decisions

- Bespoke clients for Lever/Ashby, vendored scraper for Workday: Lever and Ashby's public APIs are simple enough that owning thin clients is cheaper than adding a dependency; Workday's pagination-cap/facet-subdivision complexity is substantial and already solved and maintained upstream (jobhive, with the user's own fork tracking it).
- Lever + Ashby + dispatch refactor ship before Workday: gets multi-ATS dispatch working end-to-end on the simpler platforms first, without waiting on the vendoring/adapter work Workday requires.
- Workday's board identifier is a full careers URL rather than a short slug like `board_token` on the other three platforms — this doc intentionally leaves the field-storage decision (reuse `board_token` vs. a distinct field) to planning rather than deciding it here.

---

## Dependencies / Assumptions

- Assumes `kalil0321/ats-scrapers`' `ats-companies/lever.csv`, `ashby.csv`, and `workday.csv` datasets exist and are usable the same way `greenhouse.csv` is today (confirmed present in the dataset repo).
- Assumes jobhive-py's `WorkdayScraper` remains usable as a vendored dependency (via PyPI `jobhive-py` or the user's fork `yash10019coder/ats-scrapers`); introduces `httpx`, `pydantic`, and `pandas` as new transitive dependencies to jobborg (jobhive's package `__init__.py` imports its dataset client, which depends on `pandas`, even when only scraper classes are used).
- Assumes Lever's and Ashby's public board APIs remain unauthenticated and stable enough to support the same retry-on-transient-error pattern used for Greenhouse.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R6, R9, R11][Technical] Should `ats` choices be added incrementally per platform (Lever/Ashby now, Workday later) or all at once now with Workday's client landing after?

THey should be added incrementally

- [Affects R9, R10][Needs research] Confirm the minimal import surface needed from jobhive to use only `WorkdayScraper` without incurring the full `pandas`/dataset-client dependency weight, if avoidable.

Yes I agree with above
- [Affects Workday board storage][Technical] Whether `JobSource.board_token`/`DiscoveredBoard` reuse the same field for Workday's full-URL identifier or need a distinct field/convention.

Check accordingly for this
