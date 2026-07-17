"""Test settings — no external broker or Redis required.

Celery runs tasks eagerly (in-process), and the cache is local-memory, so the
suite depends only on the Postgres test database. This keeps CI hermetic and
prevents tests from silently depending on a reachable Redis broker.
"""
from .base import *  # noqa: F401,F403

DEBUG = False

# Tasks execute synchronously in-process; no broker connection is made.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Local-memory cache so the rematch debounce token store needs no Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "jobborg-test",
    },
}

# Faster password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
