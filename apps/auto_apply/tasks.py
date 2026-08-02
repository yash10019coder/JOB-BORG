"""Celery tasks for apps.auto_apply.

Mirrors `apps/jobs/tasks.py`'s conventions: `@shared_task(name="apps.<app>.
<verb_noun>")` naming, per-item `try/except Exception` isolation (`# noqa:
BLE001`) so a failure never crashes the worker, and `transaction.atomic()`
around the single-row `AutoApplyDraft` create (handled inside
`services.drafting._persist_draft`).

`draft_auto_apply` (U6) is the first task added to this file -- U7 extends
it with `submit_auto_apply_draft` and the staleness sweep, so this stays
deliberately minimal and easy to append to.
"""
import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from apps.jobs.models import Job

from .services.drafting import draft_for

logger = logging.getLogger(__name__)

User = get_user_model()


@shared_task(name="apps.auto_apply.draft_auto_apply")
def draft_auto_apply(user_id, job_id):
    """Draft an auto-apply attempt for `user_id` applying to `job_id`.

    Thin wrapper around `services.drafting.draft_for` -- the actual
    orchestration (schema inspection, standard-field fill, answer
    resolution, exclusion-vs-drafted decision, and the `AutoApplyDraft`
    create itself) lives there so it stays independently testable without a
    Celery task boundary.

    Follows `apps/jobs/tasks.py`'s per-item isolation posture: any failure
    (a `GreenhouseFormError` `draft_for` didn't already turn into an
    `EXCLUDED` row, an unexpected exception, etc.) is caught and logged
    rather than raised, so one bad draft trigger never surfaces as a noisy
    Celery retry storm -- even though, unlike the ingestion sweep, this
    task only ever processes one (user, job) pair per invocation.

    Returns the created `AutoApplyDraft`'s id, or `None` if drafting was a
    no-op (a concurrent duplicate trigger, see `draft_for`) or the task hit
    an unhandled error.
    """
    try:
        user = User.objects.select_related("profile").get(pk=user_id)
        job = Job.objects.get(pk=job_id)
        draft = draft_for(user, job)
    except Exception:  # noqa: BLE001 -- one failed draft trigger must not crash the worker
        logger.exception(
            "draft_auto_apply failed for user_id=%s job_id=%s", user_id, job_id
        )
        return None

    if draft is None:
        logger.info(
            "draft_auto_apply(user_id=%s, job_id=%s): no-op (an active draft "
            "already exists for this user/job).",
            user_id,
            job_id,
        )
        return None

    return draft.pk
