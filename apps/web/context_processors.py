"""Template context processors for site-wide chrome (nav badges, etc.)."""
from apps.auto_apply.models import AutoApplyDraft


def auto_apply_pending_count(request):
    """Count of the current user's unsent `DRAFTED` auto-apply drafts.

    Surfaced as a nav badge (see `templates/base.html`) so a drafted-but-
    never-sent application doesn't just silently go `STALE` once its job
    closes (`apps.auto_apply.tasks.sweep_stale_auto_apply_drafts`) without
    the user ever having been nudged back to the queue to finish it.
    """
    if not request.user.is_authenticated:
        return {}
    return {
        "auto_apply_pending_count": AutoApplyDraft.objects.filter(
            user=request.user, status=AutoApplyDraft.Status.DRAFTED
        ).count()
    }
