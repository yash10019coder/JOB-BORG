"""Cache key and invalidation for the per-user questions panel.

The panel (built in ``apps.auto_apply.services.questions_panel``) is cached
per user for an hour. Anything that can change what it shows -- a new draft, a
saved/deleted answer, a settings save -- calls
:func:`invalidate_questions_panel`. It lives here, below ``auto_apply``, so
``write_answer`` can invalidate without importing the app that builds the panel.
"""
from django.core.cache import cache
from django.db import transaction

# Bump when the cached payload's shape changes.
PANEL_CACHE_VERSION = "v1"
PANEL_CACHE_TTL = 3600


def panel_cache_key(user_id):
    return f"profile_questions_panel:{PANEL_CACHE_VERSION}:{user_id}"


def invalidate_questions_panel(user_id):
    """Drop the user's cached panel once the surrounding transaction commits
    (so a concurrent rebuild cannot cache the pre-commit state)."""
    key = panel_cache_key(user_id)
    transaction.on_commit(lambda: cache.delete(key))
