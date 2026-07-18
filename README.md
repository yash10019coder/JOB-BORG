# JobBorg

A multi-user job search automation platform. **v1** ingests jobs hourly from a
single official ATS integration (Greenhouse), classifies them with a rule-based
engine, scores them per-user against each user's profile, and surfaces ranked
recommendations. Auto-apply, referral outreach, and cold-email are planned for
later phases (schema-scaffolded, not built).

See [`docs/plans/2026-07-18-001-feat-jobborg-v1-plan.md`](docs/plans/2026-07-18-001-feat-jobborg-v1-plan.md)
for the full design.

## Stack

Django 5 · PostgreSQL + pgvector · Celery + Celery Beat + Redis · server-rendered templates.

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Brings up five core services: `db` (Postgres+pgvector), `redis`, `web` (Django on
:8000), `worker` (Celery), and `beat` (hourly ingestion + classification sweep) —
plus the [observability stack](#observability) (pgAdmin, Prometheus, Loki, Grafana).

Then, in another shell:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Register a Greenhouse board to ingest — the `board_token` is the path segment
in a company's `boards.greenhouse.io/<board_token>` careers URL:

```bash
docker compose exec web python manage.py add_job_source stripe airbnb figma
```

Validates each token against the live Greenhouse API before creating anything
(so a typo'd token fails with a clear error instead of a dead row), creates the
`Employer` if it doesn't exist yet, and skips tokens already registered. Pass
`--name "Display Name"` when registering a single token to override the
auto-derived (title-cased) employer name.

Trigger ingestion immediately instead of waiting for the hourly Beat run:

```bash
docker compose exec worker python -c \
  "from apps.jobs.tasks import ingest_all_active_sources; print(ingest_all_active_sources())"
```

Sign up at `/accounts/signup/`, set your criteria at `/profile/`, and view
ranked matches at `/`.

## Pipeline

```
ingest (hourly) -> classify (rule engine) -> match (per-user fan-out) -> recommend
```

Each stage is a decoupled Celery task. Ingestion is idempotent (keyed on
`(source_ats, source_job_id)`); classification only re-runs on content change;
matching fans out to pre-filtered active profiles and refreshes immediately when
a profile is edited.

## Observability

Local development stack for inspecting the database and watching the pipeline run:

| Service | URL | Login | Purpose |
|---|---|---|---|
| pgAdmin | http://localhost:5050 | `.env` `PGADMIN_DEFAULT_EMAIL` / `PGADMIN_DEFAULT_PASSWORD` (default `admin@jobborg.dev` / `jobborg`) | Browse the `db` Postgres instance — a `JobBorg` server is pre-registered (enter the DB password `jobborg` on first connect) |
| Grafana | http://localhost:3000 | `admin` / `.env` `GF_SECURITY_ADMIN_PASSWORD` (default `jobborg`) | Dashboards + log explorer; Prometheus and Loki datasources are pre-provisioned |
| Prometheus | http://localhost:9090 | — | Raw metrics + `/targets` scrape health |
| Loki | http://localhost:3100 | — | Raw log API (mostly queried via Grafana Explore) |

**Metrics:** `web` exposes `django-prometheus` metrics at `/metrics` (request rate,
latency, status codes). Celery workers emit task events (`-E` flag +
`CELERY_WORKER_SEND_TASK_EVENTS`) scraped by `celery-exporter`. `postgres-exporter`
and `redis-exporter` cover the two data stores. Prometheus scrapes all four every
15s — check `http://localhost:9090/targets` to confirm they're all `UP`.

**Logs:** every container's stdout is tailed by `promtail` and shipped to `loki`.
Django/Celery logs are JSON by default (`DJANGO_LOG_FORMAT=json`) so they're
queryable — e.g. in Grafana Explore, `{compose_service="web"}` or
`{compose_service="worker"} | json | levelname="ERROR"`.

**Starter dashboards** (auto-loaded in Grafana under the `JobBorg` folder):
`JobBorg: Django` (request rate/latency/errors), `JobBorg: Celery` (task
throughput/failures/runtime), `JobBorg: Infra` (Postgres/Redis health).

Try it end-to-end: trigger ingestion (see above), then watch the `JobBorg: Celery`
dashboard and Grafana Explore logs update in real time — this is the fastest way
to answer "did anything actually happen?" instead of grepping raw container logs.

This is a **development-only** setup (no auth on Prometheus/Loki, ports bound to
localhost, Promtail reads the Docker socket) — do not expose it as-is in production.

## Tests

The suite is hermetic — it needs only the Postgres test database (no broker/Redis)
because `config/settings/test.py` runs Celery eagerly with a local-memory cache:

```bash
DATABASE_URL="postgres://jobborg:jobborg@localhost:5432/jobborg" \
DJANGO_SETTINGS_MODULE=config.settings.test \
python manage.py test
```

## Layout

```
config/            # settings (base/dev/prod/test), celery, urls
apps/
  accounts/        # User (built-in) + Profile
  employers/       # Employer
  jobs/            # Job, JobSource, Greenhouse client, ingestion
  classification/  # rule engine + classification task
  matching/        # UserJobMatch, scorer, prefilter, fan-out
  applications/    # JobApplication (save/apply/dismiss)
  web/             # views, forms, templates
```
