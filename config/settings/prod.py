"""Production settings."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Fail loudly if the secret key wasn't provided in the environment.
SECRET_KEY = env("DJANGO_SECRET_KEY")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)

CELERY_TASK_ALWAYS_EAGER = False
