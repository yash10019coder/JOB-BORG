"""Development settings."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = True

# Run Celery tasks eagerly (synchronously, in-process) unless explicitly
# disabled — makes local test/dev flows deterministic without a live worker.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True

INTERNAL_IPS = ["127.0.0.1"]
