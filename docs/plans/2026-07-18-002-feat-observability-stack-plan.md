---
title: "feat: Add pgAdmin + Prometheus/Loki/Grafana observability stack"
type: feat
status: completed
created: 2026-07-18
---

# feat: Add pgAdmin + Observability Stack (Prometheus, Loki, Grafana)

> **On execution, save this plan to** `docs/plans/2026-07-18-002-feat-observability-stack-plan.md` (plan mode only permits editing the scratch plan file).

## Context

JobBorg v1 runs a 5-service Docker Compose stack (`db`, `redis`, `web`, `worker`, `beat`) with no way to inspect the database visually or observe what the app/pipeline is doing at runtime. When "I ran it twice but nothing happened" surfaced earlier, diagnosis meant grepping raw container logs by hand. This plan adds:

- **pgAdmin** — a web UI over Postgres for inspecting jobs/matches/applications during development.
- **Prometheus + exporters** — metrics from Django (per-view/DB/cache), Celery (task throughput/failures/latency), Postgres, and Redis.
- **Loki + Promtail** — centralized log aggregation from all containers, queryable in Grafana.
- **Grafana** — single pane of glass with pre-provisioned Prometheus + Loki datasources and starter dashboards.

Outcome: a developer can `docker compose up`, open Grafana, and see request rates, task success/failure, DB/Redis health, and searchable logs — plus browse the DB in pgAdmin — without wiring anything by hand.

**Confirmed scope decisions (from planning):**
- Metrics: **full app + infra** — `django-prometheus` and `celery-exporter` (app code changes) plus `postgres_exporter` and `redis_exporter`.
- Logs: **Promtail** tailing Docker container logs → Loki (no app-side log driver changes).
- Extras: **JSON logging** in Django + **provisioned** Grafana datasources and starter dashboards.

This is a **development/local observability** stack. Production hardening (auth, TLS, retention tuning, resource limits, remote write) is explicitly deferred.

---

## Scope Boundaries

**In scope:** pgAdmin service; Prometheus + 2 exporters; celery-exporter; django-prometheus wiring; Loki + Promtail; Grafana with provisioned datasources + starter dashboards; JSON logging config; env/README updates.

**Out of scope (non-goals):**
- Production auth/TLS/ingress for any of the new UIs (they bind to localhost only).
- Alerting rules / Alertmanager / paging.
- Metrics retention/storage tuning beyond sensible defaults.
- Prometheus multiprocess mode for a multi-worker gunicorn (dev uses `runserver`, single process) — see Risks.
- Distributed tracing (Tempo/OpenTelemetry).

### Deferred to Follow-Up Work
- Alertmanager + alert rules once baseline dashboards prove useful.
- Gunicorn + `prometheus_multiproc_dir` when the web tier moves off `runserver` for prod.
- Tempo/OTel tracing spanning the ingest→classify→match pipeline.

---

## Output Structure

New files land under a single `observability/` tree plus edits to existing config:

```text
observability/
  prometheus/
    prometheus.yml                      # scrape config
  loki/
    loki-config.yml                     # single-binary filesystem config
  promtail/
    promtail-config.yml                 # docker log discovery -> loki
  grafana/
    provisioning/
      datasources/datasources.yml       # Prometheus + Loki datasources
      dashboards/dashboards.yml         # dashboard provider
    dashboards/
      django.json                       # request/latency/DB
      celery.json                       # task throughput/failures
      infra.json                        # postgres + redis
  pgadmin/
    servers.json                        # pre-registered db connection
docker-compose.yml                      # +7 services, volumes (modify)
.env.example                            # new service env vars (modify)
requirements/base.txt                   # django-prometheus, python-json-logger (modify)
config/settings/base.py                 # prometheus apps/middleware, celery events, LOGGING (modify)
config/urls.py                          # /metrics endpoint (modify)
README.md                               # observability section (modify)
```

The `observability/` layout is a scope declaration; the implementer may adjust if a cleaner layout emerges. Compose service definitions are authoritative per-unit.

---

## Implementation Units

Suggested landing sequence: U1 → U2 → U3 → U4 → U5 → U6 → U7 → U8 → U9. U1–U2 are independent and low-risk; U3/U4 (app code) gate U5's scrape config; U6/U7 gate U8's datasources/dashboards.

### U1. pgAdmin service

**Goal:** Web UI over the `db` Postgres, reachable at `http://localhost:5050`, with the JobBorg server pre-registered.

**Dependencies:** none

**Files:**
- `docker-compose.yml` (modify — add `pgadmin` service + `pgadmin_data` volume)
- `observability/pgadmin/servers.json` (create)
- `.env.example` (modify — `PGADMIN_DEFAULT_EMAIL`, `PGADMIN_DEFAULT_PASSWORD`)

**Approach:** `dpage/pgadmin4` image. Env-set default login. Mount `servers.json` to `/pgadmin4/servers.json` so the `db` connection (host `db`, port 5432, user `jobborg`) is pre-registered — the password is still entered on first connect (pgAdmin does not import passwords from servers.json by design). `depends_on: db (service_healthy)`. Named volume for pgAdmin's own config so registrations persist.

**Patterns to follow:** existing `db`/`redis` service shape in `docker-compose.yml` (env, ports, healthcheck, `depends_on: condition: service_healthy`).

**Test scenarios:** `Test expectation: none — infra/config, no behavioral code.` Verification is manual (see Verification).

**Verification:** `docker compose up -d pgadmin` → `http://localhost:5050` loads, login works, the `JobBorg` server appears in the tree and connects after entering the DB password.

---

### U2. Postgres + Redis exporters

**Goal:** Prometheus-scrapable metrics endpoints for Postgres and Redis.

**Dependencies:** none

**Files:**
- `docker-compose.yml` (modify — add `postgres-exporter`, `redis-exporter` services)
- `.env.example` (modify — `DATA_SOURCE_NAME` note if not inlined)

**Approach:**
- `postgres-exporter` (`quay.io/prometheuscommunity/postgres-exporter`): env `DATA_SOURCE_NAME=postgresql://jobborg:jobborg@db:5432/jobborg?sslmode=disable`, exposes `:9187/metrics`. `depends_on: db (service_healthy)`.
- `redis-exporter` (`oliver006/redis_exporter`): env `REDIS_ADDR=redis://redis:6379`, exposes `:9121/metrics`. `depends_on: redis`.
- Ports need not be published to the host (Prometheus reaches them on the compose network) — publish only if you want to curl them directly; leave unpublished by default.

**Patterns to follow:** existing service definitions; reuse the same DB/Redis credentials already in `.env.example`.

**Test scenarios:** `Test expectation: none — infra/config.`

**Verification:** `docker compose exec prometheus wget -qO- http://postgres-exporter:9187/metrics | head` returns `pg_up 1`; same for `redis-exporter:9121` returning `redis_up 1`. (Confirmed indirectly via U5's Prometheus targets page.)

---

### U3. Django metrics instrumentation (django-prometheus)

**Goal:** Django exposes `/metrics` with per-view latency/count, plus DB and cache metrics; scrapable by Prometheus.

**Dependencies:** none (but its output is consumed by U5)

**Files:**
- `requirements/base.txt` (modify — add `django-prometheus`)
- `config/settings/base.py` (modify — `INSTALLED_APPS`, `MIDDLEWARE`)
- `config/urls.py` (modify — include metrics URLs)
- `apps/web/tests/test_metrics.py` (create)

**Approach:**
- Add `django_prometheus` to `INSTALLED_APPS`.
- Wrap `MIDDLEWARE`: `django_prometheus.middleware.PrometheusBeforeMiddleware` as the **first** entry and `django_prometheus.middleware.PrometheusAfterMiddleware` as the **last** entry — order matters for correct latency measurement.
- In `config/urls.py`, add `path("", include("django_prometheus.urls"))` so `GET /metrics` returns the exposition format. Metrics path is `/metrics`, no collision with `apps.web.urls`.
- DB/cache deeper instrumentation (swapping to `django_prometheus.db.backends.postgresql` / instrumented cache backend) is **optional** and requires overriding the engine that `env.db()` returns — defer unless the default view/process metrics prove insufficient. Do not wire by default (keeps `DATABASE_URL` env-driven config untouched).

**Patterns to follow:** `INSTALLED_APPS`/`MIDDLEWARE` ordering already in `config/settings/base.py`; existing `include(...)` usage in `config/urls.py`.

**Test scenarios:**
- Happy path: `GET /metrics` returns HTTP 200 and body contains a stable `django_http_*` metric name (e.g. `django_http_requests_total_by_view_transport_method`).
- Content type: response is the Prometheus text exposition format (`text/plain` with version), not HTML.
- Integration: after an authenticated request to `recommendations`, `/metrics` reflects a request counter for that view (verifies middleware is actually recording).
- No regression: the existing `recommendations` view still returns 200 with the Prometheus middleware wrapping the chain.

**Verification:** test suite green; `curl localhost:8000/metrics` locally shows `django_*` series.

---

### U4. Celery task metrics (celery-exporter + event emission)

**Goal:** Celery task throughput, success/failure, runtime, and queue metrics available to Prometheus.

**Dependencies:** none (consumed by U5)

**Files:**
- `docker-compose.yml` (modify — add `celery-exporter` service; add `-E` to `worker` command)
- `config/settings/base.py` (modify — enable task events)
- `config/tests/test_celery_events.py` (create)

**Approach:**
- Celery workers must emit events for the exporter to see them. Add `-E` to the `worker` command (`celery -A config worker -l info -E`) **and** set `CELERY_WORKER_SEND_TASK_EVENTS = True` and `CELERY_TASK_SEND_SENT_EVENT = True` in `base.py` (belt-and-suspenders; the settings make it robust regardless of CLI flags).
- `celery-exporter` (`ghcr.io/danihodovic/celery-exporter`): env `CE_BROKER_URL=redis://redis:6379/0` (reuse `REDIS_URL`), exposes `:9808/metrics`. `depends_on: redis`.
- Note: `CELERY_TASK_ALWAYS_EAGER` must remain **off** in the running stack for events to flow — it already is in the compose `.env` path (on only in `config.settings.test`). No test-settings change.

**Patterns to follow:** existing Celery config block in `config/settings/base.py`; existing `worker` service command in `docker-compose.yml`.

**Test scenarios:**
- Settings: `CELERY_WORKER_SEND_TASK_EVENTS` and `CELERY_TASK_SEND_SENT_EVENT` are `True` in loaded settings (guards against silent regression).
- `Test expectation (exporter/compose): none — infra`, verified live.

**Verification:** with the stack up, trigger `ingest_all_active_sources` (per README), then Prometheus target `celery-exporter` shows `celery_task_*` series with the task name.

---

### U5. Prometheus service + scrape config

**Goal:** Prometheus scrapes all four sources (web, celery-exporter, postgres-exporter, redis-exporter) and is browsable at `http://localhost:9090`.

**Dependencies:** U2, U3, U4

**Files:**
- `docker-compose.yml` (modify — add `prometheus` service + `prometheus_data` volume)
- `observability/prometheus/prometheus.yml` (create)

**Approach:** `prom/prometheus` image, mount `prometheus.yml` read-only, publish `:9090`. Scrape jobs (15s interval):
- `django` → `web:8000` (metrics path `/metrics`)
- `celery` → `celery-exporter:9808`
- `postgres` → `postgres-exporter:9187`
- `redis` → `redis-exporter:9121`
- `prometheus` → `localhost:9090` (self)

All targets resolved by Docker Compose service DNS on the shared network.

**Patterns to follow:** existing compose service + named-volume pattern.

**Test scenarios:** `Test expectation: none — infra/config.`

**Verification:** `http://localhost:9090/targets` shows all five targets `UP` (the single strongest end-to-end check for U2–U5).

---

### U6. Loki + Promtail (log aggregation)

**Goal:** All container logs shipped to Loki and queryable; Loki reachable at `http://localhost:3100`.

**Dependencies:** none (consumed by U8)

**Files:**
- `docker-compose.yml` (modify — add `loki`, `promtail` services + `loki_data` volume)
- `observability/loki/loki-config.yml` (create)
- `observability/promtail/promtail-config.yml` (create)

**Approach:**
- `loki` (`grafana/loki`): single-binary filesystem config (`loki-config.yml`) — boltdb-shipper + filesystem store, no external object storage. Publish `:3100`. Named volume for chunks/index.
- `promtail` (`grafana/promtail`): mount `/var/lib/docker/containers:/var/lib/docker/containers:ro` and `/var/run/docker.sock:/var/run/docker.sock:ro`; config uses Docker service-discovery (or the container-log file glob) to tail all compose containers, relabel with `container`/`compose_service`, and push to `http://loki:3100/loki/api/v1/push`. `depends_on: loki`.
- Promtail's docker socket mount is the standard mechanism; note it grants container-metadata access (dev-only — see Risks).

**Patterns to follow:** existing compose bind-mount style (`.:/app`), named volumes.

**Test scenarios:** `Test expectation: none — infra/config.`

**Verification:** `curl "http://localhost:3100/loki/api/v1/labels"` lists labels including `compose_service`; in Grafana (U8) an Explore query `{compose_service="web"}` returns Django logs.

---

### U7. Structured JSON logging in Django

**Goal:** Django emits one-line JSON logs (level, logger, message, timestamp) so Loki/Grafana parse and filter cleanly.

**Dependencies:** none (improves U6/U8 log quality)

**Files:**
- `requirements/base.txt` (modify — add `python-json-logger`)
- `config/settings/base.py` (modify — add `LOGGING`)
- `config/tests/test_logging.py` (create)

**Approach:** Add a `LOGGING` dict with a `json` formatter (`pythonjsonlogger.jsonlogger.JsonFormatter`) on a console handler writing to stdout, wiring root + `django` + `celery` + `apps` loggers at env-driven level (`DJANGO_LOG_LEVEL`, default `INFO`). Keep a `DJANGO_LOG_FORMAT=json|plain` env toggle so local dev can fall back to human-readable output; default `json` in containers. Promtail ships stdout regardless; JSON just makes it queryable.

**Patterns to follow:** env-driven settings pattern (`env(...)`, `env.int(...)`) already pervasive in `config/settings/base.py`.

**Test scenarios:**
- Happy path: with `DJANGO_LOG_FORMAT=json`, a record emitted through the configured handler is valid JSON containing `levelname`, `name`, `message`.
- Toggle: with `DJANGO_LOG_FORMAT=plain`, output is not JSON (human-readable), asserting the branch is honored.
- Level: `DJANGO_LOG_LEVEL=WARNING` suppresses an `INFO` record (guards env wiring).

**Verification:** `docker compose logs web` shows JSON lines; suite green.

---

### U8. Grafana with provisioned datasources + starter dashboards

**Goal:** Grafana at `http://localhost:3000` boots with Prometheus + Loki datasources already connected and 3 starter dashboards loaded — zero manual setup.

**Dependencies:** U5 (Prometheus), U6 (Loki); dashboards reference metrics from U3/U4/U2

**Files:**
- `docker-compose.yml` (modify — add `grafana` service + `grafana_data` volume)
- `observability/grafana/provisioning/datasources/datasources.yml` (create)
- `observability/grafana/provisioning/dashboards/dashboards.yml` (create)
- `observability/grafana/dashboards/django.json` (create)
- `observability/grafana/dashboards/celery.json` (create)
- `observability/grafana/dashboards/infra.json` (create)
- `.env.example` (modify — `GF_SECURITY_ADMIN_PASSWORD`)

**Approach:** `grafana/grafana` image. Mount the `provisioning/` tree to `/etc/grafana/provisioning` and the dashboards dir to the path referenced by `dashboards.yml`. Datasource provisioning declares:
- Prometheus → `http://prometheus:9090` (default datasource)
- Loki → `http://loki:3100`

Dashboard provider points at the mounted dashboards dir. Starter dashboards:
- `django.json` — request rate, p50/p95 latency, error rate by view (from `django_http_*`).
- `celery.json` — tasks succeeded/failed/retried, runtime, by task name (from `celery_task_*`).
- `infra.json` — `pg_up`/connections/tx and `redis_up`/memory/ops (from postgres & redis exporters).

Admin password via `GF_SECURITY_ADMIN_PASSWORD` env; keep default login for now. `depends_on: [prometheus, loki]`.

**Patterns to follow:** compose service + named volume + read-only config mounts (as in U5/U6).

**Test scenarios:** `Test expectation: none — provisioning/config`, verified live. Dashboard JSON validity spot-checked with `python -m json.tool` on each file.

**Verification:** `http://localhost:3000` login works; **Connections → Data sources** shows Prometheus + Loki both "working"; the 3 dashboards render live panels; a Loki Explore query returns logs.

---

### U9. Wiring, env, and docs

**Goal:** `.env.example` documents every new variable and `README.md` explains the observability stack; the full `docker compose up` brings everything healthy.

**Dependencies:** U1–U8

**Files:**
- `.env.example` (modify — consolidate all new vars with comments)
- `README.md` (modify — "Observability" section: URLs, ports, login, querying)
- `docker-compose.yml` (modify — final review: `depends_on`, volumes block, port map consistency)

**Approach:** Collect env vars introduced across units (pgAdmin login, Grafana admin password, exporter DSNs if externalized, log level/format) into `.env.example` with inline comments. Add a README section listing each UI, its localhost port, default creds, and one example task (trigger ingestion → watch celery dashboard + logs). Confirm the compose `volumes:` block lists every new named volume (`pgadmin_data`, `prometheus_data`, `loki_data`, `grafana_data`).

**Patterns to follow:** existing `.env.example` comment style; existing README "Quick start"/"Pipeline"/"Tests" section structure.

**Test scenarios:** `Test expectation: none — docs/config.`

**Verification:** fresh `cp .env.example .env && docker compose up --build` brings all 12 services up; every URL in the README table loads.

---

## Ports & Services Summary

| Service | Image (recommended, pin at impl) | Host port | Purpose |
|---|---|---|---|
| pgadmin | `dpage/pgadmin4` | 5050 | Postgres UI |
| prometheus | `prom/prometheus` | 9090 | Metrics store/scraper |
| postgres-exporter | `quay.io/prometheuscommunity/postgres-exporter` | (internal) | PG metrics |
| redis-exporter | `oliver006/redis_exporter` | (internal) | Redis metrics |
| celery-exporter | `ghcr.io/danihodovic/celery-exporter` | (internal) | Celery task metrics |
| loki | `grafana/loki` | 3100 | Log store |
| promtail | `grafana/promtail` | (internal) | Log shipper |
| grafana | `grafana/grafana` | 3000 | Dashboards |

Existing: `web` (8000), `db` (5432), `redis` (6379). Exact image tags are an implementation-time detail — pin to current stable versions (do not use `:latest` in the committed compose file).

---

## Key Technical Decisions

- **Full app instrumentation, not just infra exporters** — the "nothing happened" class of problem needs Django request and Celery task visibility, which sidecar exporters can't provide. Cost: small, well-contained edits to `base.py`/`urls.py` and the worker command.
- **Promtail over the Loki Docker log driver** — no host Docker daemon plugin install; app/compose stays portable. Trades a docker-socket mount (dev-acceptable) for zero daemon changes.
- **Provisioned datasources + dashboards** — Grafana is useful on first boot; no click-ops to reproduce setup across machines.
- **JSON logging behind an env toggle** — containers get queryable structured logs; local dev can flip to plain text. Keeps the existing env-driven settings convention.
- **Exporter ports stay internal** — Prometheus reaches them over the compose network; not publishing them to the host reduces surface area. Only human-facing UIs (pgAdmin/Prometheus/Grafana/Loki) publish ports.
- **DB/cache deep instrumentation deferred** — swapping the DB backend engine for `django_prometheus.db.backends.postgresql` fights the `env.db()` URL-driven config; default view/process metrics are enough to start.

---

## Risks & Mitigations

- **django-prometheus + multiprocess web server:** metrics are per-process. Fine under dev `runserver` (single process); under gunicorn with N workers you'd need `prometheus_multiproc_dir`. Mitigation: documented as deferred; dev stack uses `runserver`.
- **Promtail docker.sock mount:** grants read access to container metadata. Acceptable for a local dev stack; must not ship to prod as-is. Mitigation: scope boundary states dev-only; note in README.
- **Celery events overhead / EAGER mode:** if `CELERY_TASK_ALWAYS_EAGER` is on, no events flow and `celery-exporter` shows nothing. Mitigation: EAGER is off in the compose run path (on only in `config.settings.test`); U4 asserts the event flags.
- **Resource footprint:** 7 new containers roughly doubles the stack's memory. Mitigation: all are optional-to-run (`docker compose up db redis web worker beat` still works); observability services can be started on demand.
- **Loki/Grafana version drift in dashboards:** provisioned dashboard JSON can break across major Grafana versions. Mitigation: pin the Grafana image tag; keep starter dashboards minimal.

---

## Verification (End-to-End)

1. `cp .env.example .env && docker compose up --build` — all 12 services reach healthy/running.
2. **pgAdmin:** `http://localhost:5050` → log in → `JobBorg` server connects → browse `jobs`/`matching_userjobmatch` tables.
3. **Prometheus:** `http://localhost:9090/targets` → all 5 targets `UP`.
4. **Metrics flow:** trigger ingestion (`docker compose exec worker python -c "from apps.jobs.tasks import ingest_all_active_sources; print(ingest_all_active_sources())"`), then confirm `celery_task_*` series appear in Prometheus and the Celery Grafana dashboard.
5. **Grafana:** `http://localhost:3000` → Prometheus + Loki datasources "working" → 3 dashboards render live panels.
6. **Logs:** Grafana Explore → `{compose_service="web"}` returns JSON-parsed Django logs; filter by level works.
7. **Test suite:** `DATABASE_URL=... DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test` stays green (new tests: `/metrics` endpoint, celery event flags, JSON logging).
8. **Degradation:** `docker compose up db redis web worker beat` (no observability services) still runs the app normally.
