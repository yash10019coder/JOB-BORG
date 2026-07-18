---
date: 2026-07-18
topic: job-source-discovery
---

# Automated Job Source Discovery

## Summary

Add a scheduled, automated discovery pipeline that finds new Greenhouse boards via search-engine scraping, validates each candidate against the live Greenhouse API, and surfaces validated candidates to a human reviewer for approval before they become active `JobSource`s. This replaces manually running `add_job_source` per company as the only way to grow job coverage.

---

## Problem Frame

Users see "No recommendations above the threshold yet" because there simply aren't enough jobs in the database to match against, even with "show all matches" enabled. The recommendation engine and hourly ingestion sweep (`ingest_all_active_sources`) both work correctly — the bottleneck is upstream: a `JobSource` (an employer's Greenhouse board) only enters the system when someone manually runs `manage.py add_job_source <token>` for that specific company. Today only a handful of companies have been registered this way, added by hand, one CLI invocation at a time. There's no mechanism that finds new companies on its own, so coverage is capped at however many boards someone happens to type in — which is why it's stagnant.

---

## Actors

- A1. Discovery pipeline (system): the new scheduled job that searches for candidate Greenhouse board URLs, validates them, and queues the valid ones for review.
- A2. Reviewer (staff/admin): a human who reviews queued candidate boards and approves or rejects each one.
- A3. Ingestion sweep (existing system, unchanged): the existing hourly Celery task that pulls jobs from active `JobSource`s. It consumes this pipeline's output but is not modified by this work.

---

## Key Flows

- F1. Daily discovery run
  - **Trigger:** scheduled daily (Celery beat)
  - **Actors:** A1
  - **Steps:** run search queries for Greenhouse board URLs → extract candidate board tokens from the results → skip tokens that are already an active `JobSource` or already pending review → validate each remaining new token against the live Greenhouse API (the same check `add_job_source` performs today) → for tokens that validate, create a pending discovered board → discard tokens that fail validation → record run results (found / validated / already-known / failed) as a log entry and a metric
  - **Outcome:** new candidate boards are queued for review; the run's outcome is visible even when it finds zero new boards or a query fails
  - **Covered by:** R1, R2, R3, R4, R5, R6, R11

- F2. Reviewer approves or rejects a discovered board
  - **Trigger:** reviewer opens a pending discovered board
  - **Actors:** A2
  - **Steps:** reviewer sees the candidate token, derived employer name, and validation result → approves or rejects it → approval creates the `JobSource` (and `Employer` if needed), identical to what `add_job_source` creates today → rejection marks the candidate so future discovery runs don't re-suggest it
  - **Outcome:** approved boards become eligible for the next hourly ingestion sweep; rejected ones stop reappearing
  - **Covered by:** R7, R8, R9, R10

---

## Requirements

**Discovery**
- R1. A scheduled job runs daily and searches for Greenhouse board URLs, independent of and in addition to the existing hourly ingestion sweep.
- R2. The job extracts candidate Greenhouse board tokens from the search results.
- R3. Candidate tokens that already have an active `JobSource`, or are already pending review, are skipped without a redundant validation call.
- R4. Each remaining new candidate token is validated against the live Greenhouse API, reusing the same validation `add_job_source` already performs.
- R5. Tokens that fail validation are discarded — they are never queued for review.
- R6. A single failed search query or a single failed token validation does not abort the rest of that day's run, mirroring the per-source failure isolation the hourly ingestion sweep already uses.

**Review**
- R7. Tokens that pass validation are stored as pending discovered boards, visible to a reviewer, until explicitly approved or rejected.
- R8. A reviewer can approve a pending discovered board; approval creates a `JobSource` (and `Employer` if one doesn't exist) exactly as `add_job_source` does today.
- R9. A reviewer can reject a pending discovered board; rejected tokens are not re-suggested by future discovery runs. A rejected token that later warrants inclusion (e.g. a company that becomes legitimate after being rejected as a duplicate/test board) is not automatically resurfaced — re-adding it goes through the existing manual `add_job_source` path, the same as any company today.
- R10. No discovered board becomes an active `JobSource` — and therefore no discovered board's jobs can reach recommendations — without an explicit reviewer approval.

**Observability**
- R11. Each discovery run's results (boards found, validated, already-known, run failures) are logged and exposed as a metric, so a stall (e.g. from being blocked by a search engine) is visible on the existing Grafana dashboards rather than failing silently.

---

## Acceptance Examples

- AE1. **Covers R3.** Given a candidate token that already has an active `JobSource`, when a discovery run encounters that token again, no duplicate pending discovered board is created and no validation call is made for it.
- AE2. **Covers R4, R5.** Given a candidate token that returns an unparseable or unreachable response from the Greenhouse API, when validation runs, the token is discarded and never appears in the review queue.
- AE3. **Covers R6, R11.** Given a discovery run whose search step is blocked or returns no results, when the run completes, the failure is logged and the run reports zero boards found via the metric rather than the scheduled task crashing or failing silently.
- AE4. **Covers R8, R10.** Given a pending discovered board, when a reviewer approves it, a `JobSource` is created and becomes eligible for the next hourly ingestion sweep — no jobs from that board appear in recommendations before that approval happens.

---

## Success Criteria

- Job coverage grows steadily without anyone manually running `add_job_source` — reviewer throughput, not company research, becomes the limiting factor.
- A reviewer can look at a pending discovered board and decide approve/reject without reading code or logs.
- Discovery run health (boards found, stalled, blocked) is visible without SSHing into a worker or grepping raw logs.

---

## Scope Boundaries

- Other ATS platforms (Lever, Workday, Ashby, etc.) — Greenhouse only for now.
- Auto-approval of discovered boards — every discovered board requires explicit human review before it goes live; no fast path that skips review.
- A fixed numeric coverage target (e.g. "500 active sources") — success is judged by trend and by whether users start seeing recommendations, not a specific company count.
- Paid/official search APIs — explicitly rejected for this iteration in favor of direct scraping.
- Any dedicated review UI beyond what's needed for a reviewer to see and act on pending boards — the surface itself (e.g. Django admin vs. a custom screen) is a planning-level decision.
- Self-serve company/board submission by end users — reviewers are internal staff, not product users, in this iteration.

---

## Key Decisions

- **Direct search-engine scraping over a paid search API:** accepts the ToS and reliability risk (possible IP blocks, fragile result parsing) to avoid ongoing per-query cost. If this proves unreliable in practice, revisit toward a paid API or an alternate source (e.g. public "companies on Greenhouse" lists).
- **Human review gate before go-live:** search-scraped candidates carry more false-positive risk than a curated list, so review protects real users from bad or irrelevant matches — even though it caps growth speed at reviewer throughput rather than automation throughput.
- **Discovery is a new daily job, separate from the existing hourly ingestion sweep:** it manages growth of the `JobSource` registry itself; the job-pulling loop that already exists (`ingest_all_active_sources`) is unchanged.

---

## Dependencies / Assumptions

- Assumes the existing Greenhouse validation call used by `add_job_source` (fetching jobs for a board token) is reusable as-is for discovery's validation step.
- Assumes there is no existing "pending/review queue" concept anywhere in the codebase today (confirmed absent in this scan of `apps/jobs`) — this introduces one net new.
- Assumes reviewers will be internal staff/admins operating through an internal tool, not end users of the product.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1, R2][Needs research] What search query pattern(s) actually surface genuinely new Greenhouse boards day-to-day (broad enumeration vs. targeted company-name lists), and what query volume is sustainable before triggering blocks?
- [Affects R7][Technical] Where does the reviewer act on pending discovered boards — Django admin (fastest, consistent with existing `apps/jobs/admin.py` usage) or a dedicated review screen?
- [Affects R1][Technical] What scraping mechanism (HTTP client, headless browser, rotating IPs/user-agents, request pacing) best balances reliability against the accepted ToS risk?

---

## Deferred / Open Questions

### From 2026-07-18 review

- **No cheaper alternative evaluated before committing to a full pipeline** — Problem Frame / Key Decisions (P1, product-lens, confidence 75)

  The document jumps straight from "not enough jobs" to "build a daily scraping-and-review pipeline" without weighing whether a narrower move — a curated seed list of ~100-200 known Greenhouse-using companies added through the existing `add_job_source` path, or loosening the recommendation match threshold — would deliver most of the coverage benefit for far less build and ongoing legal-risk cost.

  <!-- dedup-key: section="problem frame key decisions" title="no simpler alternative threshold loosening curated seed list evaluated before committing to a full scraping pipeline" evidence="this replaces manually running add_job_source per company as the only way to grow job coverage" -->

- **Discovered-board content is never treated as untrusted external input** — Requirements / Key Flows (F1, F2) (P1, security-lens, confidence 75)

  Validation only confirms a token resolves to a live, parseable Greenhouse-shaped API response — it says nothing about whether the job content itself (titles, descriptions) is legitimate versus spam, scam postings, or malicious markup. No requirement treats discovered-board content as untrusted until reviewed, even though it eventually reaches real users as recommendations.

  <!-- dedup-key: section="requirements key flows f1 f2" title="no acknowledgment that discovered board content is untrusted attacker influenceable data eventually shown to end users" evidence="validate each remaining new token against the live greenhouse api the same check add_job_source performs today" -->

- **No way for a reviewer to catch a spoofed or squatted board** — Requirements R3, R7 (P1, security-lens, confidence 75)

  Dedup only compares token strings and validation only confirms a board is live and Greenhouse-shaped — neither establishes the board belongs to the company it claims to be. A typosquatted or impersonating board would pass both checks identically to a legitimate one, and the reviewer is shown only "candidate token, derived employer name, and validation result" — exactly the surface a spoof would also present cleanly.

  <!-- dedup-key: section="requirements r3 r7" title="dedup and validation dont establish company identity legitimacy and reviewer lacks information to catch a spoofedsquatted board" evidence="candidate tokens that already have an active jobsource or are already pending review are skipped without a redundant validation call" -->

- **Scraping-vs-API cost rationale compares sticker price, not total cost** — Key Decisions (P2, adversarial + product-lens, confidence 100)

  The document justifies direct scraping over a paid search API to "avoid ongoing per-query cost," while accepting "possible IP blocks, fragile result parsing" as risk — but those accepted risks are themselves ongoing engineering-maintenance costs (proxy/IP rotation, retry logic, parser upkeep), not a one-time trade. The comparison as written weighs sticker price against total cost, not total against total.

  <!-- dedup-key: section="key decisions" title="direct search engine scraping over a paid search api compares scrapings sticker price to an apis metered price not total operating cost" evidence="direct search engine scraping over a paid search api accepts the tos and reliability risk possible ip blocks fragile result parsing to avoid ongoing per query cost" -->
