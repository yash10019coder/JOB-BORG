# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Antigravity/Gemini,
Codex, Cursor, etc.) when working with code in this repository. It is
tool-agnostic — any agent operating in this repo should read it first.

## Commands

### Run the app (Docker — primary workflow)

```bash
cp .env.example .env
docker compose up --build
```

Brings up `db` (Postgres+pgvector), `redis`, `web` (Django on :8000), `worker`
(Celery), `beat` (Celery Beat), plus an observability stack (pgAdmin,
Prometheus, Loki, Grafana — see README for URLs/logins).

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py add_job_source stripe airbnb figma
```

Trigger ingestion immediately instead of waiting for the hourly Beat tick:

```bash
docker compose exec worker python -c \
  "from apps.jobs.tasks import ingest_all_active_sources; print(ingest_all_active_sources())"
```

### Tests

The suite is hermetic — only needs the Postgres test database (Celery runs
eager in-process, cache is local-memory, see `config/settings/test.py`):

```bash
DATABASE_URL="postgres://jobborg:jobborg@localhost:5432/jobborg" \
DJANGO_SETTINGS_MODULE=config.settings.test \
python manage.py test
```

Run a single app's tests or a single test case/method the normal Django way,
e.g.:

```bash
python manage.py test apps.locations
python manage.py test apps.locations.tests.test_engine.NormalizeLocationTests
python manage.py test apps.locations.tests.test_engine.NormalizeLocationTests.test_bare_abbrev_collision
```

Test conventions: plain `django.test.TestCase` (no factory library), the
`responses` library for HTTP mocking (`@responses.activate`), JSON fixtures
under each app's `tests/fixtures/`.

### Other useful management commands

- `manage.py backfill_locations --dry-run --json --strict` — re-normalize
  `Job`/`Profile` locations against the current dataset version without
  writing; `--strict` exits non-zero on any diff (used as a pre-deploy safety
  check, not a hard CI gate — canonical GeoNames renames can trigger it on
  real data, see issue history).
- `apps/locations/geodata_generation.py`'s management command regenerates the
  versioned location dataset (`apps/locations/geodata/vN.yaml`) from GeoNames
  exports.

## Architecture

### Pipeline

```
ingest (hourly, per JobSource) -> classify (rule engine) -> match (per-user fan-out) -> recommend
```

Each stage is a decoupled Celery task (see `CELERY_BEAT_SCHEDULE` in
`config/settings/base.py`). Ingestion is idempotent, keyed on
`(source_ats, source_job_id)`. Classification only re-runs on content change
(`Job.needs_classification`). Matching fans out to pre-filtered active
profiles on new/changed jobs, and separately rematches a recent job window
when a profile is edited (debounced — see `REMATCH_DEBOUNCE_SECONDS`).

### App layout and dependency direction

```
apps/
  accounts/        # User (Django built-in) + Profile
  employers/       # Employer
  locations/       # location-string normalization — dependency-free leaf
  jobs/            # Job, JobSource, DiscoveredBoard, ATS ingestion clients
  classification/  # rule engine + classification task
  matching/        # UserJobMatch, scorer, prefilter, fan-out
  applications/    # JobApplication (save/apply/dismiss)
  web/             # views, forms, templates
```

`apps.locations` and `apps.classification` are both "dependency-free leaf"
apps: pure, deterministic, side-effect-free logic loaded once via
`lru_cache` over a versioned static dataset, no DB/network access, imported
by `jobs`/`web`/`matching` but never importing back from them. When adding
logic to either, preserve that boundary.

### Multi-ATS ingestion (`apps/jobs/ingestion/`)

`dispatch.py`'s `CLIENT_REGISTRY` maps `JobSource.ATS` values to client
classes (Greenhouse, Lever, Ashby, Workday — Workday's scraper is vendored
from `jobhive`, not installed as a dependency, to avoid pulling in `pandas`).
Adding a new ATS is a one-line registry addition plus a client class — no
other call site (`ingest_source`, `discover_boards`, `register_job_source`,
the `add_job_source` command) needs to change. Each client raises its own
`*Unavailable`/`*ParseError` exceptions and is DB-free/standalone, reusable
for both ingestion and board-token validation during discovery.

Board discovery (`apps/jobs/tasks.py:discover_boards`, daily) scrapes Bing for
new `boards.<ats>.../<token>`-style URLs, validates candidates live against
the ATS API, and queues them as `DiscoveredBoard` rows for explicit Django
admin review/approval — discovery never auto-approves a source.

### Location normalization (`apps/locations/engine.py`)

`normalize_location()` resolves free-text location strings (job postings,
profile target locations) into structured `{city, region, country}` via a
versioned, checked-in YAML dataset (`geodata/vN.yaml`, GeoNames-derived as of
v2+). `CURRENT_LOCATION_ALIAS_VERSION` is the *only* signal
`sweep_stale_locations` uses to find rows needing re-normalization — bump it
whenever the resolution *logic* changes, not just when the dataset file
changes, or previously-resolved rows go silently stale. Ambiguous bare tokens
(a name that resolves to more than one country/region, or a country/region
name colliding with itself) are excluded from the alias dicts entirely and
fail closed to unresolved, rather than guessing — city-level same-type
collisions are the sole exception, tie-broken by GeoNames feature-code then
population. The guiding invariant across this engine: **a confidently wrong
resolution is worse than an unresolved one.**

### Matching fan-out (`apps/matching/services.py`)

Two entry points share scoring/prefilter logic:
- `match_job` (job-centric): one newly-classified/updated job vs. all active
  profiles, DB-pre-filtered before scoring.
- `rematch_profile_obj` (profile-centric): one edited profile vs. a recent
  window of open jobs (`REMATCH_JOB_WINDOW_DAYS`).

Disqualification (profile narrowed past a job, or job closed) deletes the
`UserJobMatch` row — the job leaves that user's list. A saved
`JobApplication` lives in a separate table and is never touched by
rematch/disqualification.

### Celery queues and scheduling

All periodic work is registered as static `CELERY_BEAT_SCHEDULE` entries in
`config/settings/base.py`, synced into `PeriodicTask` by
`django_celery_beat`'s `DatabaseScheduler`. Domain-tunable constants (batch
sizes, debounce windows, discovery caps) are `env.int(...)`-backed settings
near the schedule, not hardcoded in task bodies.

## Stack

Django 5 · PostgreSQL + pgvector · Celery + Celery Beat + Redis ·
server-rendered templates (no frontend framework/build step). See
`requirements/base.txt` / `requirements/dev.txt` for exact dependency
versions and rationale comments (e.g. why the Workday scraper is vendored
instead of depending on `jobhive-py`).

## Docs and conventions worth knowing

- `docs/plans/` holds one plan-per-feature (frontmatter `status:
  active|completed`); `docs/brainstorms/` holds the requirements docs plans
  originate from. Check open GitHub issues against these before starting
  work that overlaps a "Deferred to Follow-Up Work" / "Scope Boundaries"
  section — deferred items are generally already tracked as issues.
- `docs/solutions/` captures institutional learnings from past bugs (e.g.
  `logic-errors/onsite-only-location-filter-ignores-target-locations.md`).
  Worth checking before touching matching/prefilter/location code.
- Model conventions to mirror: `TextChoices` inner classes for status/enum
  fields, `is_`/`needs_`-prefixed booleans, explicit `on_delete` +
  `related_name` on FKs, `created_at`/`updated_at` timestamp pairs,
  `models.UniqueConstraint` in `Meta.constraints` (not `unique_together`).
- Task naming convention: `@shared_task(name="apps.<app>.<verb_noun>")`.
- This is a **development-only** observability setup (no auth on
  Prometheus/Loki, Promtail reads the Docker socket) — do not carry it into
  production as-is.
