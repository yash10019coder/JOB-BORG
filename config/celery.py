"""Celery application for JobBorg.

Broker/result backend come from Django settings (Redis). Tasks are
auto-discovered from every installed app's ``tasks`` module.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("jobborg")

# Pull all CELERY_* settings from Django config.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks.py in each installed app.
app.autodiscover_tasks()


@app.task(name="config.debug_add")
def debug_add(x, y):
    """Trivial task proving the enqueue -> execute -> result round-trip."""
    return x + y
