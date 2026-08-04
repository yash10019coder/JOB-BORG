"""Base settings shared across environments.

Env-driven via django-environ. Override per-environment in dev.py / prod.py.
"""
from pathlib import Path

import environ
from celery.schedules import crontab

# config/settings/base.py -> config/settings -> config -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

# Read .env if present (docker-compose also injects env vars directly).
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third-party
    "django_celery_beat",
    "django_prometheus",
    # Local apps
    "apps.accounts",
    "apps.employers",
    "apps.locations",
    "apps.jobs",
    "apps.classification",
    "apps.matching",
    "apps.applications",
    "apps.auto_apply",
    "apps.web",
]

MIDDLEWARE = [
    # First/last per django-prometheus: brackets the full request/response
    # cycle so latency includes everything in between.
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database — DATABASE_URL, e.g. postgres://user:pass@host:5432/db
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://jobborg:jobborg@localhost:5432/jobborg",
    ),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# File storage -- resume uploads (U1). No FileField existed anywhere in this
# codebase before this unit, so there was no prior storage-backend decision
# to inherit. Local/dev falls back to FileSystemStorage under MEDIA_ROOT
# (only coincidentally durable/multi-process-safe via Docker Compose's shared
# bind mount); setting AWS_STORAGE_BUCKET_NAME switches to S3-compatible
# object storage (django-storages) so the uploaded file is readable by both
# the web process (upload) and Celery workers (resume parsing here, and the
# Playwright submission task in U7) in a real deployment, not just a single
# host. AWS_S3_ENDPOINT_URL lets this point at any S3-compatible provider
# (e.g. MinIO/R2), not only AWS.
# ---------------------------------------------------------------------------
MEDIA_URL = env("MEDIA_URL", default="/media/")
MEDIA_ROOT = BASE_DIR / "media"

AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
if AWS_STORAGE_BUCKET_NAME:
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-east-1")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
    AWS_S3_FILE_OVERWRITE = False
    AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=True)
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

# Resume upload constraints (U1), enforced at the model layer
# (Profile.validate_resume_file) before a file ever reaches the parsing
# task/library -- a storage/DoS control and a first line of defense against
# malicious uploads (disguised executables, malformed PDFs).
RESUME_MAX_UPLOAD_SIZE_BYTES = env.int(
    "RESUME_MAX_UPLOAD_SIZE_BYTES", default=10 * 1024 * 1024  # 10 MB
)
RESUME_ALLOWED_CONTENT_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
]
RESUME_ALLOWED_EXTENSIONS = [".pdf", ".docx", ".txt"]

# Bounded execution for the resume text-extraction task (U1) -- allowlisting
# file type doesn't guarantee a well-formed file; PDF/DOCX parsers have a
# history of resource-exhaustion bugs independent of content type.
RESUME_PARSE_TASK_TIME_LIMIT_SECONDS = env.int(
    "RESUME_PARSE_TASK_TIME_LIMIT_SECONDS", default=120
)
RESUME_PARSE_TASK_SOFT_TIME_LIMIT_SECONDS = env.int(
    "RESUME_PARSE_TASK_SOFT_TIME_LIMIT_SECONDS", default=90
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth redirects (used by the web UI in U11/U12).
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "recommendations"
LOGOUT_REDIRECT_URL = "login"

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL",
    default=env("REDIS_URL", default="redis://localhost:6379/0"),
)
CELERY_RESULT_BACKEND = env(
    "CELERY_RESULT_BACKEND",
    default="redis://localhost:6379/1",
)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# Task events power celery-exporter's Prometheus metrics (throughput,
# failures, runtime). Belt-and-suspenders alongside `-E` on the worker CLI.
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True

# Named queues per pipeline stage so a slow stage never blocks another.
CELERY_TASK_DEFAULT_QUEUE = "default"

# The DatabaseScheduler syncs these static entries into PeriodicTask on startup.
CELERY_BEAT_SCHEDULE = {
    "ingest-all-sources-hourly": {
        "task": "apps.jobs.ingest_all_active_sources",
        "schedule": crontab(minute=0),  # top of every hour
    },
    "discover-boards-daily": {
        "task": "apps.jobs.discover_boards",
        "schedule": crontab(minute=0, hour=3),  # off-peak
    },
    "classification-sweep": {
        "task": "apps.classification.sweep_unclassified",
        "schedule": crontab(minute="*/5"),  # catch anything the event path missed
    },
    "location-alias-sweep": {
        "task": "apps.locations.sweep_stale_locations",
        "schedule": crontab(minute="*/5"),  # cheap no-op until the alias table version bumps
    },
    "auto-apply-staleness-sweep": {
        "task": "apps.auto_apply.sweep_stale_auto_apply_drafts",
        "schedule": crontab(minute="*/5"),  # same cadence as location-alias-sweep
    },
}

# ---------------------------------------------------------------------------
# JobBorg domain constants
# ---------------------------------------------------------------------------
# Batch bound for the classification task (U8).
CLASSIFICATION_BATCH_SIZE = env.int("CLASSIFICATION_BATCH_SIZE", default=200)
# Recency window (days) for profile-centric rematch (U10).
REMATCH_JOB_WINDOW_DAYS = env.int("REMATCH_JOB_WINDOW_DAYS", default=30)
# Debounce delay (seconds) collapsing rapid successive profile saves into one
# rematch execution (U10).
REMATCH_DEBOUNCE_SECONDS = env.int("REMATCH_DEBOUNCE_SECONDS", default=10)
# Upsert batch size for the matching fan-out.
MATCH_BULK_BATCH_SIZE = env.int("MATCH_BULK_BATCH_SIZE", default=500)
# Batch bound for the location backfill/sweep (shared by both).
LOCATION_BACKFILL_BATCH_SIZE = env.int("LOCATION_BACKFILL_BATCH_SIZE", default=500)
# Cap on how many not-yet-known boards discover_boards will validate and
# queue for review in a single run (R1/R7) -- the candidate dataset can
# return thousands of tokens at once, and reviewer throughput, not
# candidate-source volume, is meant to be the growth bottleneck.
DISCOVERY_MAX_NEW_BOARDS_PER_RUN = env.int("DISCOVERY_MAX_NEW_BOARDS_PER_RUN", default=50)

# ---------------------------------------------------------------------------
# Auto-apply: LLM answer inference (apps.auto_apply.llm) -- provider-agnostic;
# Anthropic Claude is the only registered implementation in this slice (see
# apps/auto_apply/llm/base.py's CLIENT_REGISTRY).
# ---------------------------------------------------------------------------
AUTO_APPLY_LLM_PROVIDER = env("AUTO_APPLY_LLM_PROVIDER", default="anthropic")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
NVIDIA_API_KEY = env("NVIDIA_API_KEY", default="")
# Secondary tiebreaker only -- consulted after the deterministic evidence-
# groundedness check has already passed (see
# apps/auto_apply/llm/base.py:resolve_answers).
AUTO_APPLY_CONFIDENCE_THRESHOLD = env.float("AUTO_APPLY_CONFIDENCE_THRESHOLD", default=0.75)

# ---------------------------------------------------------------------------
# Auto-apply: CAPTCHA solving (apps.auto_apply.captcha)
# ---------------------------------------------------------------------------
# Provider key looked up in apps.auto_apply.captcha.base.CAPTCHA_SOLVER_REGISTRY.
# Unset by default: this slice ships the pluggable interface only, with no
# vendor registered, so lookups return None and callers fail closed (R14).
AUTO_APPLY_CAPTCHA_PROVIDER = env("AUTO_APPLY_CAPTCHA_PROVIDER", default="")
# Placeholder credential for a future registered provider (e.g. 2Captcha,
# Anti-Captcha). Unused until a provider is actually registered.
AUTO_APPLY_CAPTCHA_API_KEY = env("AUTO_APPLY_CAPTCHA_API_KEY", default="")

# ---------------------------------------------------------------------------
# Auto-apply: send flow (apps.auto_apply.tasks.submit_auto_apply_draft)
# ---------------------------------------------------------------------------
# How long a draft may sit in SENDING before `sweep_stale_auto_apply_drafts`
# treats it as stuck (crashed worker / lost task enqueue) and recovers it to
# FAILED. Well beyond the expected "seconds to over a minute" submission
# round trip (including a possible CAPTCHA-solve attempt).
AUTO_APPLY_SENDING_TIMEOUT_SECONDS = env.int(
    "AUTO_APPLY_SENDING_TIMEOUT_SECONDS", default=300
)

# ---------------------------------------------------------------------------
# Auto-apply: submission debug artifacts (apps.auto_apply.greenhouse_form)
# ---------------------------------------------------------------------------
# Directory where GreenhouseFormClient writes a screenshot + accessibility-
# tree snapshot when a submission raises (rejection, schema mismatch, no
# post-submit confirmation signal found). Empty by default -- opt-in via env
# since these can capture PII (the applicant's own submitted answers) and
# accumulate on disk; set to enable diagnosing real-world submission
# failures after the fact instead of only from live-attached debugging.
AUTO_APPLY_DEBUG_ARTIFACT_DIR = env(
    "AUTO_APPLY_DEBUG_ARTIFACT_DIR", default=str(BASE_DIR / "media" / "auto_apply_debug")
)

# ---------------------------------------------------------------------------
# Credential encryption (apps.accounts.crypto) -- at-rest encryption for
# stored secrets (starting with the IMAP app password used to auto-solve
# Greenhouse's post-submit email verification, see
# docs/plans/2026-08-04-001-feat-auto-apply-greenhouse-email-verification-plan.md).
# A list of urlsafe-base64 Fernet keys: the first encrypts, and
# `MultiFernet` tries all of them in order when decrypting, so rotation is
# "prepend a new key, keep the old one(s) until every ciphertext has been
# re-encrypted." No default beyond an empty list, and deliberately NOT
# derived from SECRET_KEY (which has an insecure dev default above) --
# encrypt_secret()/decrypt_secret() raise ImproperlyConfigured if this is
# unset, rather than silently using a weak key. Generate a key with
# apps.accounts.crypto.generate_key().
# ---------------------------------------------------------------------------
CREDENTIAL_ENCRYPTION_KEYS = env.list("CREDENTIAL_ENCRYPTION_KEYS", default=[])

# ---------------------------------------------------------------------------
# Cache — Redis-backed so the rematch debounce token is shared across workers.
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("CACHE_URL", default="redis://localhost:6379/3"),
    },
}

# ---------------------------------------------------------------------------
# Logging — JSON lines to stdout by default so Promtail/Loki can parse and
# filter by level/logger. Set DJANGO_LOG_FORMAT=plain for human-readable
# console output during local (non-container) development.
# ---------------------------------------------------------------------------
DJANGO_LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO")
DJANGO_LOG_FORMAT = env("DJANGO_LOG_FORMAT", default="json")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "plain": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": DJANGO_LOG_FORMAT if DJANGO_LOG_FORMAT in ("json", "plain") else "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": DJANGO_LOG_LEVEL,
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": DJANGO_LOG_LEVEL, "propagate": False},
        "celery": {"handlers": ["console"], "level": DJANGO_LOG_LEVEL, "propagate": False},
        "apps": {"handlers": ["console"], "level": DJANGO_LOG_LEVEL, "propagate": False},
    },
}
