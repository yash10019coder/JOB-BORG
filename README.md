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

Brings up five services: `db` (Postgres+pgvector), `redis`, `web` (Django on
:8000), `worker` (Celery), and `beat` (hourly ingestion + classification sweep).

Then, in another shell:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Register a Greenhouse board to ingest (via the admin at `/admin/` or the shell):

```python
from apps.employers.models import Employer
from apps.jobs.models import JobSource
emp = Employer.objects.create(name="Greenhouse", slug="greenhouse")
JobSource.objects.create(ats="greenhouse", board_token="greenhouse", employer=emp)
```

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
