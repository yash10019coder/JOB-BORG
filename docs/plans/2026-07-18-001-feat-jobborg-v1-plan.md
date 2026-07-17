---
title: JobBorg — Job Search Automation Platform (v1)
type: feat
status: completed
date: 2026-07-18
---

# JobBorg — Job Search Automation Platform

## Summary

Build a multi-user job search automation platform. V1 ingests jobs hourly from a single official ATS integration (Greenhouse), classifies them with a rule-based engine, scores them per-user against each user's profile criteria, and surfaces ranked recommendations in a simple Django web UI. No auto-apply, referral outreach, or cold-email in v1 — those are deferred to later phases, with the schema built so they don't require a later migration.

---

## Context

The user has been job hunting for ~1.5 months and found the manual grind (searching, filtering, filling repetitive application forms, chasing referrals) exhausting. The goal is a platform that automates this end-to-end: ingest jobs continuously, classify and match them against a user's stated criteria, recommend the best matches, and — in later phases — auto-apply, request real employee referrals, and reach out to recruiters. This is being built as a **multi-user product** for other job seekers, not a personal one-off tool, which was confirmed explicitly after an early draft schema assumed single-user.

The `jobborg` directory was completely empty when this plan was written — this is a from-scratch build with no existing code, stack, or conventions to inherit.

Both the referral flow and the cold-email flow are staying in the product roadmap (not cut) — the only thing settled during brainstorming was *how* each is implemented:
- The "referral" flow is **real human referral outreach** (finding an actual employee and asking them to vouch for the user) — not creating a duplicate/alt-account application to game an employer's or ATS's dedup logic. The duplicate-account framing was raised and rejected as fraud/ToS-violating; the real-outreach version is what's being built.
- Cold-email contacts come from **manual entries + recruiter info harvested off job postings during ingestion** — not a purchased/scraped bulk recruiter database, which was flagged as a real CAN-SPAM/GDPR/deliverability risk. Manual + harvested sourcing is what's being built.

---

## Product Scope

**V1 (this build):** hourly ingestion from a single official ATS integration (Greenhouse to start) → rule-based classification (tags computed once per job) → per-user matching against each user's profile criteria → ranked recommendations in a simple web UI. **No auto-apply in v1** — matching quality needs to be proven trustworthy before the system is allowed to act on a user's behalf.

**Deferred to later phases (schema should not preclude them, but they are not built now):**
- Bulk auto-apply using a per-user bank of past application Q&A answers (with a feedback write-back loop so the bank grows over time).
- Referral outreach: round-based real-employee outreach (round 1 = 2 contacts, round 2+ = 4 new contacts each), auto-closing other open attempts once one contact agrees, with a self-tuning wait-time (starts at "until end of day," moves to a rolling average of real reply times once enough data exists).
- Cold-emailing recruiter contacts + open/reply/bounce tracking.
- Broad scraping across job boards/career pages, rate-limit tuning, and full compliance gating (deliberately deferred — leaning on official ATS APIs only for now keeps this low-risk).

---

## Tech Stack

- **Python / Django** — built-in auth solves multi-user account management immediately; Django admin gives free CRUD/inspection over jobs/matches/applications during early development without building internal tooling.
- **PostgreSQL + pgvector** — relational schema plus embedding similarity search for the answers bank, without standing up a separate vector database.
- **Celery + Redis** — background workers and the hourly ingestion schedule (Celery Beat), with separate queues per pipeline stage so a slow stage never blocks another.
- **Frontend:** server-rendered Django templates (optionally + HTMX) for the recommendations list and basic profile/apply-status UI — no SPA framework needed for v1.
- **Deployment:** Docker Compose (web, worker, beat, Postgres, Redis) — sufficient for v1 scale (single ATS source, one company/board to start).

---

## Data Model

The key correction from an earlier single-user draft: **match scoring is a relationship between a job and a user's profile, not a property of the job.** Buckets below classify every table as global (shared across users), per-user, or a join table bridging the two.

**Global / shared:**
- `jobs` — one row per posting, shared by all users. `source_ats`, `source_job_id` (unique together for idempotent upserts), `title`, `description`, `employer` FK, location fields, `is_remote` (explicit boolean), `status` (open/closed), `classification_tags` (computed once, not per-user), `scraped_at`.
- `employers` — one row per company.
- `contacts` — single shared table for every real person at a company that either flow can reach out to (name, email, employer FK, `contact_type`: `employee` or `recruiter`, `source`: manual / job_posting / referral_search). Replaces having two near-identical tables (`employee_contacts` for referral targets, `cold_email_contacts` for recruiters) — the referral worker filters `contact_type = employee`, the cold-email worker filters `contact_type = recruiter`; a person could in principle carry both roles without duplicate rows. Keyed on email to avoid duplicates. **Not built in v1** — scaffolded in Phase 5.

**Per-user (tenant-scoped):**
- `users` — first-class auth (Django's built-in user model).
- `profiles` — one per user: matching criteria (target titles, `target_tags` — the skill/keyword list scored against a job's `classification_tags`, target locations, min salary, remote preference, excluded employers), an `is_active` flag gating whether the profile participates in matching fan-out at all, plus who-you-are fields.
- `job_applications` — unique on `(user_id, job_id)`; different users can independently apply to the same job.
- `referral_attempts` — scoped via `job_application_id`, so implicitly per-user; tracks round number, contact, status, timestamps. **Not built in v1.**
- `answers_bank` — strictly per-user with embeddings for similarity matching; must never leak across users in similarity search (privacy/correctness requirement, worth an explicit test). **Not built in v1.**
- `email_events` — the engagement log for outreach *sent by the platform*: one row per open/reply/bounce, linked back to whichever `contact_outreach_log` row it belongs to. Drives `referral_attempts.status` transitions and the referral wait-time self-tuning. **Not built in v1.**

**Join table (the core multi-user fix):**
- `user_job_matches` — `(user_id, job_id)` unique, holds `match_score`, `match_status`, `matched_tags`, `computed_at`. Replaces putting `match_score`/`match_status` directly on `jobs`. Built in v1 (U9).

**New table needed for safe cross-user referral/cold-email behavior (not built in v1):**
- `contact_outreach_log` — `(contact_id, user_id, job_application_id, contacted_at, outcome)`. Prevents two different users' referral or cold-email flows from messaging the same real person twice in a short window. Referral/cold-email contact-selection queries must look across *all users'* outreach history for a given contact, not just the current user's. `email_events` rows reference this log, not the `contacts` table directly.

---

## Worker Architecture (decoupled, queue-per-stage)

1. **Ingestion worker** (hourly, Celery Beat) — pulls from the Greenhouse integration, upserts `jobs` keyed on `(source_ats, source_job_id)` (idempotent — safe to re-run), marks postings no longer present in the feed as closed. **v1 scope note:** recruiter-contact harvesting into `contacts` is part of the full roadmap for this worker but is deferred to Phase 5 along with the `contacts` table itself — v1's ingestion worker (U6) only upserts jobs/employers and detects closures. Runs once regardless of user count.
2. **Classification worker** (batched, rule-based) — computes `classification_tags` once per job (seniority, tech stack, industry, remote-ness). Bootstrapped by running an LLM over a sample of past job descriptions to discover tag patterns, hand-converting those into a cheap rule engine, and re-sampling periodically (e.g. monthly) to catch drift — not an LLM call per job. Runs once per job, never per (job, user).
3. **Matching worker (the multi-user fan-out point)** — on each newly-classified or updated job, computes/upserts a `user_job_matches` row against every active profile (pre-filtered on broad criteria like location/remote-pref before finer tag comparison, to avoid an O(jobs × users) blow-up as the platform grows). Also triggered when a user edits their profile, so recommendations refresh without waiting for the next ingestion cycle.
4. **Apply worker** (deferred past v1, but schema should support it) — per-job, per-user trigger; pulls from `answers_bank`, fills the form via an ATS-specific adapter (Greenhouse first).
5. **Referral worker** (deferred past v1) — round-based outreach using `contacts` (filtered to `contact_type = employee`) filtered against `contact_outreach_log` (so nobody gets double-messaged across users), auto-closes other open attempts once one contact agrees, self-tunes wait time from the rolling average of real reply times across all users once enough data exists.
6. **Cold email worker** and **tracking worker** (deferred past v1) — same cross-user rate-limiting pattern via `contact_outreach_log`; tracking worker polls periodically for opens/replies.

---

## Scope Boundaries

- No auto-apply, referral outreach, or cold-email sending in v1 — the workers above describe the full roadmap; only ingestion, classification, and matching (items 1–3) are built now.
- No broad multi-platform scraping (LinkedIn, Indeed, career pages) — a single official ATS integration (Greenhouse) only.
- No rate-limit tuning or compliance gating beyond what's structurally designed into the deferred tables — those take effect only once the referral/cold-email workers are actually built.

### Deferred to Follow-Up Work

- Phase 5 (schema scaffolding for `contacts`, `contact_outreach_log`, `answers_bank`, `email_events`): a follow-up unit once v1's matching quality is validated.
- Auto-apply, referral outreach, and cold-email workers themselves: a separate planning pass once v1 ships and real usage validates the matching approach.

---

## Implementation Units (v1 Build)

Django app layout:

```text
config/                  # project package: settings, celery, root urls/wsgi/asgi
apps/
  accounts/               # users (built-in auth) + profiles
  employers/               # employer/company model
  jobs/                   # jobs model, Greenhouse client, ingestion tasks
  classification/          # rule engine + classification task
  matching/               # user_job_matches + fan-out task
  applications/            # job_applications + save/dismiss/apply actions
  web/                    # views, templates, HTMX, recommendation UI
```

Suggested landing sequence: U1 → U2 → (U3 ∥ U4) → U5 → U6 → U7 → U8 → U9 → U10 → U11 → U12.

---

### Phase 0 — Foundations

### U1. Docker Compose + service scaffold
- **Files:** `docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.env.example`, `requirements/base.txt`, `requirements/dev.txt`, `scripts/entrypoint.sh`, `scripts/wait-for-postgres.sh`
- **Approach:** `pgvector/pgvector:pg16` image so pgvector is available without a custom build. Services: `db`, `redis`, `web`, `worker`, `beat` — one Dockerfile, different `command`s. Env-driven config (`DATABASE_URL`, `REDIS_URL`, `DJANGO_SETTINGS_MODULE`); entrypoint waits for Postgres.
- **Test expectation:** none -- pure infra scaffolding, no application behavior yet.
- **Verification:** fresh clone + `cp .env.example .env` + `docker compose up` brings all five services healthy with no app code yet.

### U2. Django project skeleton + settings + Celery wiring
- **Dependencies:** U1
- **Files:** `manage.py`, `config/settings/{base,dev,prod}.py`, `config/celery.py`, `config/{urls,wsgi,asgi}.py`, `config/tests/test_celery_smoke.py`
- **Approach:** `django-environ` for env-driven settings; Celery app pulls broker/result backend from Redis, `autodiscover_tasks()`; `django-celery-beat` for a DB-backed, editable schedule. A `debug_add(x, y)` task proves the round-trip.
- **Test scenarios:** dev settings import cleanly against example env; `debug_add` enqueues and returns the correct result through Redis; `migrate` runs cleanly against the pgvector-enabled Postgres.
- **Verification:** `manage.py check` passes, Beat starts with the DB scheduler, debug task round-trips.

### U3. Core shared models: employers + jobs (schema only)
- **Dependencies:** U2
- **Files:** `apps/employers/{models,admin}.py`, `apps/employers/tests/test_models.py`, `apps/jobs/{models,admin}.py` (includes `JobSource`), `apps/jobs/tests/test_models.py`
- **Approach:** `Employer`: name, canonical slug/domain. `JobSource`: `ats` (e.g. `greenhouse`), `board_token`, `employer` FK — the DB-driven registry U6 iterates over; resolving a job's employer means looking up its `JobSource.employer`, not fuzzy-matching a company name string out of the API payload (a Greenhouse board is single-company, so this is a clean 1:1 lookup, not a dedup heuristic). `Job`: `source_ats`, `source_job_id` with `UniqueConstraint(source_ats, source_job_id)`, title, description, `employer` FK, location fields, `is_remote` (explicit boolean, not inferred from free-text location) so U7's remote-tag rule and U9's remote-pref pre-filter/scorer have a reliable field to key on, `status` (open/closed, default open) so U6 can mark postings that disappear from the feed as closed and U12 can exclude them from recommendations — without this, closed postings linger in recommendations indefinitely, `classification_tags` (JSONField, default empty list), `scraped_at`, timestamps. Add a nullable pgvector `embedding` column now even though v1 doesn't use it — cheap to add today, avoids a later migration when semantic matching arrives. GIN index on `classification_tags`, indexes on location/`is_remote`/`status` fields (needed by U10's pre-filter).
- **Test scenarios:** duplicate (source_ats, source_job_id) raises IntegrityError; same source_job_id under a different source_ats is allowed; new job defaults to `classification_tags == []`, `status == open`, and null embedding; employer-deletion-with-jobs behavior is explicit (PROTECT or SET_NULL, tested either way); a `JobSource` resolves to exactly one `Employer` regardless of how the job's raw payload spells the company name.
- **Verification:** migrations apply, constraints/indexes exist, admin lists both models.

### U4. accounts: profiles model + built-in auth wiring
- **Dependencies:** U2
- **Files:** `apps/accounts/{models,admin,signals}.py`, `apps/accounts/tests/test_models.py`
- **Approach:** Django's built-in `User` is the `users` table (decision — see Key Decisions). `Profile`: OneToOne to User, `target_titles`/`target_tags`/`target_locations`/`excluded_employers` (JSON lists), `min_salary` (nullable int), `remote_pref` (enum), `is_active` flag (gates matching fan-out). `target_tags` is the field U9's scorer intersects against a job's `classification_tags` to produce `matched_tags` — without it there is nothing on the profile side to overlap with. Post-save signal auto-creates an empty Profile on user creation.
- **Test scenarios:** creating a User creates exactly one Profile via signal; new Profile has empty-list/default field values; a second Profile for the same user is rejected; editing `target_titles`/`target_tags` persists as JSON lists.
- **Verification:** migrations apply, signal fires on user creation, admin inline works.

---

### Phase 1 — Ingestion

### U5. Greenhouse API client (pure, no DB)
- **Dependencies:** U2
- **Files:** `apps/jobs/ingestion/{greenhouse_client,normalizers,exceptions}.py`, `apps/jobs/tests/fixtures/greenhouse_board.json`, `apps/jobs/tests/test_greenhouse_client.py`
- **Approach:** Greenhouse public Job Board API, GET with bounded retry/backoff on 5xx/429. Normalizer maps Greenhouse JSON → a plain dict matching `Job`/`Employer` fields. Typed exceptions (`GreenhouseUnavailable`, `GreenhouseParseError`) rather than leaking raw HTTP client errors. Fully DB-free and fixture-testable.
- **Deferred implementation-time question:** whether the Greenhouse Job Board API response for a given board is paginated or returned as a single payload is an execution-time detail to confirm against the live API/docs during implementation, not assumed here — the client's public shape (returns a full normalized list for a board) should hold either way, but the pagination-handling test scenario below should be adjusted or dropped once that's confirmed.
- **Test scenarios:** fixture board parses into correct normalized dicts; missing optional fields (location/salary) normalize to sensible nulls without crashing; malformed payload raises `GreenhouseParseError`; transient 500-then-200 recovers via retry, exhausted retries raise `GreenhouseUnavailable`; 429 respects backoff; network timeout surfaces as a typed exception; if the live API proves paginated, multi-page responses concatenate correctly and stop at the last page.
- **Verification:** client returns correct normalized structures for the committed fixture and raises typed errors on every failure mode, with zero database access.

### U6. Ingestion task + idempotent upsert + hourly Beat schedule
- **Dependencies:** U3, U5
- **Files:** `apps/jobs/ingestion/upsert.py`, `apps/jobs/tasks.py`, `apps/jobs/ingestion/config.py`, `apps/jobs/migrations/` (JobSource migration, part of U3's app but landing whenever the first board is registered)
- **Approach:** Upsert service does `update_or_create`-style writes keyed on the unique constraint inside a transaction; employer resolved via the job's `JobSource.employer` (see U3 — a direct FK lookup, not name matching). Distinguishes new / changed / unchanged jobs via a content hash (title/description/location) so classification only re-runs when something actually changed — adopt a `needs_classification` boolean flag **plus** the content hash (event-driven enqueue from here, and a periodic sweep in U8 catches anything missed) as the signaling mechanism between ingestion and classification. **Closure detection:** for each `JobSource`, any existing open `Job` row for that source not present in the current fetch is marked `status = closed` — otherwise closed postings would linger in recommendations indefinitely. Board list lives in `JobSource` (a DB table, not a settings list) so boards can be added without a redeploy. Hourly Beat schedule.
- **Test scenarios:** first ingest creates N jobs/employers all flagged for classification; re-ingesting identical data creates zero new rows and flags nothing (true idempotency); a changed job's description updates the row and re-flags it; a new job in a second run creates only that row; two jobs from the same `JobSource` resolve to the same employer via the FK lookup; a job present in a prior run but absent from the current fetch is marked `closed`; a job that reappears after being closed is reopened (or explicitly stays closed per a stated decision — pick one and test it); one board's client failure doesn't abort other boards; a mid-batch error rolls back without partial writes; affected job ids are enqueued for classification exactly once per changed job.
- **Verification:** hourly schedule registered; re-running the task against the fixture is a no-op; changed jobs are detected and flagged; closed postings are marked and excluded from later matching; one board's failure doesn't affect others.

---

### Phase 2 — Classification

### U7. Rule engine (pure, data-driven)
- **Dependencies:** U2
- **Files:** `apps/classification/engine.py`, `apps/classification/rulesets/v1.yaml` (or equivalent declarative ruleset), `apps/classification/rule_types.py`, `apps/classification/tests/test_engine.py`, `apps/classification/tests/fixtures/sample_jobs.json`
- **Approach:** Declarative ruleset (keyword-any/keyword-all/regex/field-equals/salary-threshold rule types), versioned so the periodic re-sampling pass swaps in a new ruleset version without code changes. The bootstrap LLM pass that *derives* the ruleset is an offline, one-time human activity — no runtime LLM calls, and no code for it beyond a documented note.
- **Test scenarios:** keyword rule matches ("kubernetes" → devops tag); regex seniority rule matches word-boundary-correctly ("senior" matches, "senioritis" does not); field rule (`is_remote=true` → "remote" tag) independent of text; multiple matching rules return all tags, deduped and stably ordered; no match returns empty list without crashing; case-insensitive matching; two ruleset versions produce differing tag sets as expected; identical input twice yields identical output.
- **Verification:** engine produces expected tags for committed sample jobs, fully deterministic, zero network/DB calls.

### U8. Classification task (batched, consumes newly-ingested jobs)
- **Dependencies:** U3, U6, U7
- **Files:** `apps/classification/tasks.py`, `apps/classification/services.py`, `apps/classification/tests/test_tasks.py`
- **Approach:** Selects jobs flagged `needs_classification` in bounded batches, runs U7's engine, writes `classification_tags`, clears the flag, stamps ruleset version. Enqueues matching (U10) only for jobs whose tags actually changed vs. the prior value — re-classification with an unchanged ruleset is a no-op downstream. Runs both event-driven (per U6's enqueue) and as a periodic sweep (Beat, every few minutes) to catch anything missed.
- **Test scenarios:** batch classifies flagged jobs and clears flags; already-classified unchanged jobs are skipped; a job whose tags change triggers matching, one whose tags are identical does not; batch size bounds a single invocation, sweep drains the remainder across runs; classified jobs record the ruleset version used; running the task twice with no new data is a no-op with no matching enqueued.
- **Verification:** flagged jobs get correct tags and cleared flags, matching enqueues only on real tag changes, repeated runs are no-ops.

---

### Phase 3 — Matching (highest-risk unit — most test coverage)

### U9. user_job_matches model + matching scorer (pure)
- **Dependencies:** U3, U4
- **Files:** `apps/matching/{models,admin,constants}.py`, `apps/matching/scoring.py`, `apps/matching/prefilter.py`, `apps/matching/tests/{test_scoring,test_prefilter,test_models}.py`
- **Approach:** `UserJobMatch`: user FK, job FK, `UniqueConstraint(user, job)`, `match_score`, `match_status`, `matched_tags` (JSON — the score's explanation), `computed_at`; index on `(user, -match_score)` for ranked retrieval. `match_status` is threshold-derived: a single module-level `MATCH_SCORE_THRESHOLD` constant; `match_status = "recommended"` when `match_score >= MATCH_SCORE_THRESHOLD`, otherwise `"below_threshold"`. U12's recommendation view filters on `match_status = "recommended"`, not on raw score — this keeps the cutoff a single named constant rather than a magic number duplicated across the scorer and the view. Scorer is pure: takes a profile-criteria snapshot (including `target_tags`) and a job snapshot, returns a weighted score (title match, tag overlap between `target_tags` and the job's `classification_tags`, location/remote fit, salary floor) plus `matched_tags` — the actual intersection of `target_tags` and `classification_tags`, i.e. the specific tags that drove the score, not a separate free-standing concept. Pre-filter predicate encodes the cheap broad gates (location/remote-pref/excluded-employer/min-salary) used to shrink candidates before scoring — both fully DB-free and unit-testable.
- **Test scenarios:** strong match scores above threshold with `match_status = recommended` and correct `matched_tags` (the true intersection of `target_tags` and `classification_tags`); zero tag-overlap job scores near floor with `match_status = below_threshold`; excluded-employer job rejected by pre-filter regardless of tag fit; remote-only profile vs. onsite job (`is_remote = False`) filtered by pre-filter; below-min-salary job filtered/penalized per an explicit decision, unknown-salary jobs handled explicitly (not silently dropped); score scales monotonically with tag overlap; matched_tags contains exactly the intersecting tags, no extras and no tags absent from the job's own `classification_tags`; empty `target_tags`/empty-criteria profile yields defined (not crashing) behavior; identical inputs yield identical outputs; a second UserJobMatch for the same (user, job) raises IntegrityError.
- **Verification:** model and constraint migrate; scorer/pre-filter produce correct, deterministic, explainable outputs with no DB access; the threshold constant is the single source of truth for the recommended/below_threshold boundary.

### U10. Matching fan-out task (the multi-user fan-out point)
- **Dependencies:** U8, U9
- **Files:** `apps/matching/tasks.py` (`match_job_to_profiles`, `rematch_profile`), `apps/matching/services.py`, `apps/matching/signals.py`, `apps/matching/tests/{test_match_job_to_profiles,test_rematch_profile,test_fanout_integration}.py`
- **Approach:** Two entry points. **(a) Job-centric** (`match_job_to_profiles`, triggered by U8 when a job's tags change): a DB-level pre-filter query (indexed columns — location/`is_remote`/excluded-employer/salary), further restricted to `status = open` jobs, narrows active profiles to a small candidate set *before* scoring, then upserts `UserJobMatch` rows in bounded batches (`bulk_create` with conflict handling), with `match_status` set per U9's threshold. **(b) Profile-centric** (`rematch_profile`, triggered by the Profile post-save signal): re-scores just this one profile against a recent, still-open job window (e.g. last 30 days), so recommendations refresh immediately after a profile edit without waiting for the next ingestion cycle — debounced so rapid successive edits collapse to one execution. **Narrowing-profile decision:** when a profile edit means a job no longer qualifies, the existing `UserJobMatch` row is deleted (not downgraded or flagged) — the job simply disappears from that user's recommendations; if the user had separately saved/applied to it, that record lives independently in `job_applications` and is unaffected. **Job-closure decision:** a job marked `closed` by U6 has its `UserJobMatch` rows deleted the same way. Both entry points upsert (never plain-insert) so concurrent job- and profile-triggered runs touching the same (user, job) never collide; inactive profiles are always skipped.
- **Test scenarios:** fan-out happy path (N profiles, only pre-filter-passing ones get match rows with correct scores and correct `match_status`); idempotent re-run (same rows updated, not duplicated); re-classification changes update existing rows' score/matched_tags/computed_at/match_status in place; inactive profiles never get match rows; pre-filter demonstrably limits scoring invocations (no O(jobs×users) full scan); excluded-employer profiles get no match row; closed jobs are excluded from new fan-out and existing matches for a newly-closed job are removed; profile edit re-matches only the recent, open-job window, not the full historical table; narrowing a profile so a previously-matching job no longer qualifies deletes that job's UserJobMatch row while leaving any existing `job_applications` row untouched; new user with an empty job window completes cleanly with zero rows; concurrent job-centric and profile-centric runs on the same (user, job) pair produce exactly one row, no IntegrityError; rapid successive profile saves debounce to a bounded number of rematch executions; large fan-out batches stay bounded (not one query per profile); a zero-tag job matches nothing meaningful without crashing; resulting rows support ranked retrieval via the `(user, -match_score)` index.
- **Verification:** newly-classified jobs fan out only to pre-filtered active profiles against open postings with correct match rows and status; profile edits immediately re-match within the recent window and correctly drop disqualified or closed matches; re-runs and concurrent runs never duplicate or error; the pre-filter demonstrably prevents full user×job scans.

---

### Phase 4 — Recommendations UI

### U11. Auth + profile setup UI
- **Dependencies:** U4, U10, U2
- **Files:** `apps/web/{urls,views,forms}.py`, `templates/base.html`, `templates/registration/login.html`, `templates/web/profile_form.html`, `apps/web/tests/test_auth_and_profile.py`
- **Approach:** Django's built-in auth views for login/logout. `ProfileForm` (ModelForm) editing target criteria, login-required. Saving triggers the Profile post-save signal → U10's `rematch_profile`, so recommendations refresh right after setup.
- **Test scenarios:** anonymous access to profile/recommendations redirects to login; valid form submission persists parsed list fields correctly; saving the profile enqueues `rematch_profile`; invalid input (e.g. malformed min_salary) re-renders the form with errors and does not save; user A cannot view or edit user B's profile.
- **Verification:** users can log in, edit their criteria, and saving both persists and kicks off a re-match.

### U12. Ranked recommendation list + save/dismiss/mark-applied actions
- **Dependencies:** U9, U10, U11
- **Files:** `apps/applications/{models,admin}.py`, `apps/web/views.py` (extend), `apps/web/urls.py` (extend), `templates/web/recommendations.html`, `templates/web/_job_card.html`, `apps/applications/tests/test_models.py`, `apps/web/tests/test_recommendations.py`
- **Approach:** `JobApplication`: user FK, job FK, `UniqueConstraint(user, job)`, `status` (saved/applied/dismissed), timestamps — distinct from (and unaffected by) `match_status`. Recommendation view queries the current user's `UserJobMatch` rows where `match_status = "recommended"` (per U9's threshold — this is what keeps below-threshold noise out of the list), ordered by `-match_score`, joined to jobs/employers, excluding jobs with a `dismissed` `JobApplication`, paginated. Each job card shows matched_tags as the score's explanation plus save/dismiss/mark-applied actions (POST, login-required, CSRF) that idempotently upsert a `JobApplication`.
- **Test scenarios:** ranked list renders in descending match_score, only `recommended`-status matches, for the logged-in user only; a `below_threshold` match never appears in the list even though the row exists; matched_tags explanation appears on each card; save creates a `JobApplication(status=saved)`; dismiss records dismissed and removes the job from the default list; mark-applied is idempotent on repeat clicks (unique constraint, no duplicate row); save-then-applied updates the same row rather than creating a new one; user A cannot see or act on user B's matches/applications; empty-matches state renders a friendly empty list, not an error; action endpoints reject anonymous/CSRF-invalid requests.
- **Verification:** a logged-in user sees their jobs ranked with score explanations, and save/dismiss/mark-applied persist idempotently and update the list accordingly.

---

### Phase 5 — Post-v1 scaffolding (not required for launch, schema-only)

Stand up (but do not wire workers for) `contacts` (unified employee/recruiter contact table), `contact_outreach_log`, `answers_bank`, and `email_events` before the apply/referral/cold-email workers themselves are built, so later phases don't force another migration on the sensitive tables.

*Verify:* confirm no query path lets one user's `answers_bank` rows leak into another user's similarity search results — this is a privacy/correctness requirement worth an explicit test even at scaffolding time.

---

## Key Decisions

- **Built-in Django `User` is the `users` table** — no custom user model. This must hold before the first `migrate`; swapping to a custom user model later requires a full data migration, so if there's ever a reason to want one, it needs to surface now, not after Phase 0 lands.
- **`needs_classification` flag + content hash** (not direct-enqueue-only) is the ingestion→classification signal — supports both event-driven triggering from U6 and a periodic sweep in U8 that catches anything missed, and keeps re-runs idempotent.
- **`JobSource` is a DB table, not a settings list** — new Greenhouse boards can be added without a redeploy.
- **pgvector `embedding` column added to `jobs` now**, left null in v1 — cheap to add today, avoids a migration when semantic matching arrives later.
- **Narrowing a profile deletes the disqualified `UserJobMatch` row** rather than downgrading or flagging it — the job disappears from recommendations; any existing `job_applications` record (saved/applied) is a separate table and is unaffected.
- **`contacts` unification (from earlier brainstorming):** the referral-target and cold-email-recruiter tables are one `contacts` table with a `contact_type` field (`employee` / `recruiter`), not two separate tables — same person could hold either role without duplicate rows. This table is scaffolded in Phase 5 but not built until the referral/cold-email workers themselves are.
- **`match_status` is threshold-derived, not freeform:** a single `MATCH_SCORE_THRESHOLD` constant decides `recommended` vs `below_threshold` (see U9) — this field existed in earlier drafts without a defined purpose; it now has one, and the recommendation view (U12) filters on it rather than on a raw score cutoff duplicated in the view layer.
- **`is_remote` and `status` (open/closed) are explicit `Job` fields, not inferred from free text or omitted** — the matching pre-filter and scorer (U9/U10) need a reliable remote signal, and without closure detection (U6), closed postings would linger in recommendations indefinitely.
- **`Profile.target_tags` added** — the earlier data model had nothing on the profile side for `matched_tags` (a job-side concept) to intersect with; `target_tags` is that missing counterpart.
- **Employer resolution is a direct `JobSource.employer` FK lookup, not company-name matching** — a Greenhouse board is single-company, so this is a clean, non-fuzzy join rather than a dedup heuristic.

---

## Risks

- **Greenhouse API surface is assumed, not yet verified against live docs** — exact pagination behavior, rate limits, and field availability (e.g. whether structured location/salary data is reliably present) should be confirmed against the real API early in U5, since the client's retry/backoff and normalization logic depend on it.
- **Matching quality is unproven until real usage** — the weighted scorer (title/tag/location/salary) is a reasonable first cut, not a validated formula; the threshold constant will likely need tuning once real users see real recommendations, and the Success Criteria's "plausibly matches" bar is inherently subjective until then.
- **Fan-out cost grows with both job volume and user count** — the pre-filter bounds this in principle (U10), but the actual query performance at scale is untested until there's real data volume; worth revisiting if either ingestion volume or user count grows significantly beyond initial expectations.
- **Compliance/reputational risk is deferred, not eliminated** — Phase 5's `contact_outreach_log` cross-user rate-limiting and the manual/harvested-only cold-email sourcing are the mitigations already designed in, but they only take effect once those workers are actually built; this is a reminder to build them before enabling real outreach, not before v1.

---

## Success Criteria

- A user can sign up, set their target criteria, and within one ingestion+classification+matching cycle see a ranked list of real Greenhouse job postings that plausibly match their stated criteria — without ever seeing another user's matches.
- Two users with different criteria against the same underlying job pool see different, correctly-scored recommendation lists (the concrete proof that the multi-user architecture works, not just the single-user happy path).
- Editing a profile updates that user's recommendations without waiting for the next hourly ingestion run.
- Re-running ingestion or classification against unchanged upstream data never creates duplicate rows or reprocesses unchanged jobs — the pipeline is safe to re-run at any point.
- An implementer can execute U1 → U12 in the given sequence without needing to invent product behavior, scope boundaries, or scoring semantics not already pinned down above.

---

## End-to-End Verification

1. `docker compose up` — full stack (web, worker, beat, Postgres+pgvector, Redis) starts clean.
2. Create two user accounts through the UI with meaningfully different profile criteria (e.g. different target locations or remote preference).
3. Trigger ingestion manually (or wait for the hourly Beat run) against one real Greenhouse board — confirm jobs and employers are created, then re-run and confirm zero duplicates.
4. Confirm classification tags populate on the ingested jobs and matching fans out `user_job_matches` rows correctly and only for the two accounts created — with different scores per account reflecting their different criteria.
5. Log in as each user separately and confirm the recommendation list, save/dismiss/mark-applied actions, and profile-edit-triggered re-match all behave as specified above, with no cross-user data leakage anywhere in the flow.

---

## Notes for Future Planning

This plan covers the full v1 build (Phases 0–4) at implementation-unit detail, ready for `ce-work` or manual execution. Phase 5 (auto-apply, referral outreach, cold-email) is deliberately schema-scaffolded but not built — a separate planning pass is warranted once v1's matching quality is validated with real users, per the original brainstorm's phased rollout.
