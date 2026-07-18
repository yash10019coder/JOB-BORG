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
    "apps.jobs",
    "apps.classification",
    "apps.matching",
    "apps.applications",
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
