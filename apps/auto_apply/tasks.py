"""Celery tasks for apps.auto_apply.

Mirrors `apps/jobs/tasks.py`'s conventions: `@shared_task(name="apps.<app>.
<verb_noun>")` naming, per-item `try/except Exception` isolation (`# noqa:
BLE001`) so a failure never crashes the worker, and `transaction.atomic()`
around single-row writes (handled inside `services.drafting._persist_draft`
for the draft create, and inline in `submit_auto_apply_draft` below for the
`JobApplication` upsert + draft status write).

`draft_auto_apply` (U6) was the first task added to this file. U7 adds
`submit_auto_apply_draft` (the Send flow) and `sweep_stale_auto_apply_drafts`
(the Celery Beat staleness/stuck-SENDING sweep).
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.applications.models import JobApplication
from apps.jobs.models import Job

from .captcha.base import get_solver
from .greenhouse_form.client import GreenhouseFormClient
from .greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
)
from .greenhouse_form.field_mapping import schema_from_dict
from .models import AutoApplyDraft
from .services.drafting import draft_for

# Bounds how long a single submit_auto_apply_draft run may take (browser
# automation + a possible CAPTCHA-solve round trip) -- deliberately shorter
# than AUTO_APPLY_SENDING_TIMEOUT_SECONDS so a task that's genuinely running
# long gets killed by Celery before the staleness sweep would otherwise
# reset its still-in-flight draft to FAILED out from under it.
_SUBMIT_SOFT_TIME_LIMIT_SECONDS = max(
    30, settings.AUTO_APPLY_SENDING_TIMEOUT_SECONDS - 60
)
_SUBMIT_TIME_LIMIT_SECONDS = _SUBMIT_SOFT_TIME_LIMIT_SECONDS + 30

logger = logging.getLogger(__name__)

User = get_user_model()


def _reason_code_for(exc: GreenhouseFormError) -> str:
    """Map a submission-time exception to `AutoApplyDraft.ReasonCode`.

    Used instead of substring-matching `str(exc)` later in the UI layer
    (`apps/web/views.py`) -- the exception type is already the structured
    signal; discarding it into free text and re-deriving it from prose
    elsewhere would let the two drift out of sync silently.
    """
    if isinstance(exc, GreenhouseFormChallenged):
        return AutoApplyDraft.ReasonCode.CAPTCHA_CHALLENGED
    if isinstance(exc, GreenhouseFormSchemaMismatch):
        return AutoApplyDraft.ReasonCode.SCHEMA_MISMATCH
    return AutoApplyDraft.ReasonCode.SUBMISSION_FAILED


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


@shared_task(
    name="apps.auto_apply.submit_auto_apply_draft",
    time_limit=_SUBMIT_TIME_LIMIT_SECONDS,
    soft_time_limit=_SUBMIT_SOFT_TIME_LIMIT_SECONDS,
)
def submit_auto_apply_draft(draft_id):
    """Drive the real Greenhouse submission for a `SENDING` `AutoApplyDraft`.

    The Send view (U8) is responsible for the atomic `DRAFTED -> SENDING`
    guard before enqueueing this task -- this task assumes it's normally
    only ever invoked on a `SENDING` draft, but defensively no-ops (log +
    return) if that's not what it finds on load, e.g. a crashed-worker/
    lost-enqueue case already recovered by `sweep_stale_auto_apply_drafts`,
    rather than assuming the precondition still holds.

    On success: `JobApplication` is upserted to `Applied` via
    `update_or_create` (deliberately not `get_or_create` -- a `JobApplication`
    row for this (user, job) may already exist in `Saved`/`Dismissed` status
    from the user's prior manual action, and `get_or_create` would silently
    leave that pre-existing row's status untouched), linked onto
    `draft.job_application`, and `draft.status` becomes `APPLIED` -- all in
    one `transaction.atomic()` block so it's all-or-nothing.

    On any `GreenhouseFormError`: `draft.status` becomes `FAILED` with
    `error_message` populated from the exception; `JobApplication` is left
    untouched (R14).

    Returns the draft's id, or `None` if the draft no longer exists or
    wasn't `SENDING` on load.
    """
    try:
        draft = AutoApplyDraft.objects.select_related("user", "job").get(pk=draft_id)
    except AutoApplyDraft.DoesNotExist:
        logger.warning("submit_auto_apply_draft: draft_id=%s no longer exists.", draft_id)
        return None

    if draft.status != AutoApplyDraft.Status.SENDING:
        logger.info(
            "submit_auto_apply_draft(draft_id=%s): status is %s, not SENDING; "
            "no-op (already recovered or resolved by something else).",
            draft_id,
            draft.status,
        )
        return None

    job = draft.job

    # Lazy half of R15's staleness handling: a best-effort guard, not a
    # guarantee -- the browser automation (and possible CAPTCHA-solve round
    # trip) that follows can itself take real time, so a job can still close
    # in the gap between this check and the submission actually landing.
    # The proactive sweep (`sweep_stale_auto_apply_drafts`) is the other
    # half of the mitigation, not a full fix for the race.
    if job.status != Job.Status.OPEN:
        draft.status = AutoApplyDraft.Status.STALE
        draft.save(update_fields=["status", "updated_at"])
        logger.info(
            "submit_auto_apply_draft(draft_id=%s): job %s is no longer open "
            "(status=%s); marking draft STALE instead of submitting.",
            draft_id,
            job.pk,
            job.status,
        )
        return draft.pk

    # `draft.answers` holds label -> {value, needs_review, category, reason}
    # (see services/drafting.py); GreenhouseFormClient.submit() only wants
    # label -> value.
    answers = {label: entry.get("value") for label, entry in (draft.answers or {}).items()}
    expected_schema = schema_from_dict(draft.form_schema_snapshot)

    form_client = GreenhouseFormClient()
    try:
        form_client.submit(
            job.source_url,
            answers,
            expected_schema=expected_schema,
            captcha_solver=get_solver(),
        )
    except GreenhouseFormError as exc:
        draft.status = AutoApplyDraft.Status.FAILED
        draft.error_message = str(exc)
        draft.reason_code = _reason_code_for(exc)
        draft.save(update_fields=["status", "error_message", "reason_code", "updated_at"])
        logger.warning(
            "submit_auto_apply_draft(draft_id=%s): submission failed: %s", draft_id, exc
        )
        return draft.pk
    except Exception:  # noqa: BLE001 -- an unexpected failure must not crash the worker
        logger.exception(
            "submit_auto_apply_draft(draft_id=%s): unexpected error during submission.",
            draft_id,
        )
        draft.status = AutoApplyDraft.Status.FAILED
        draft.error_message = "Unexpected error during submission."
        draft.reason_code = AutoApplyDraft.ReasonCode.UNEXPECTED_ERROR
        draft.save(update_fields=["status", "error_message", "reason_code", "updated_at"])
        return draft.pk

    with transaction.atomic():
        job_application, _ = JobApplication.objects.update_or_create(
            user=draft.user,
            job=draft.job,
            defaults={"status": JobApplication.Status.APPLIED},
        )
        # Guard against sweep_stale_auto_apply_drafts having reset this
        # draft to FAILED (stuck-SENDING recovery) while this submission
        # was still genuinely in flight, not actually stuck -- an
        # unconditional save() here would silently overwrite that FAILED
        # back to APPLIED, and worse, mask that the timeout was too
        # aggressive relative to how long a real submission can take.
        updated = AutoApplyDraft.objects.filter(
            pk=draft.pk, status=AutoApplyDraft.Status.SENDING
        ).update(
            job_application=job_application,
            status=AutoApplyDraft.Status.APPLIED,
            updated_at=timezone.now(),
        )

    if not updated:
        logger.warning(
            "submit_auto_apply_draft(draft_id=%s): submission succeeded and "
            "JobApplication %s -> Applied, but the draft was no longer "
            "SENDING (likely reset by the staleness sweep while this "
            "submission was still in flight) -- draft status left as-is; "
            "JobApplication is the source of truth for the real outcome.",
            draft_id,
            job_application.pk,
        )
        return draft.pk

    logger.info(
        "submit_auto_apply_draft(draft_id=%s): submission succeeded; JobApplication %s -> Applied.",
        draft_id,
        job_application.pk,
    )
    return draft.pk


@shared_task(name="apps.auto_apply.sweep_stale_auto_apply_drafts")
def sweep_stale_auto_apply_drafts():
    """Celery Beat task -- the proactive half of R15, plus stuck-`SENDING`
    recovery.

    Two independent sweeps, both cheap bulk `.update()` calls (mirroring
    `apps/locations`' `sweep_stale_locations` no-op-until-needed cadence):

    1. `DRAFTED` drafts whose `Job.status` has flipped to `CLOSED` are
       transitioned to `STALE` (R15's proactive half; `submit_auto_apply_draft`
       is the lazy send-time half).
    2. `SENDING` drafts older than `AUTO_APPLY_SENDING_TIMEOUT_SECONDS` are
       reset to `FAILED` with a "timed out / recovered from stuck state"
       `error_message` -- covers a worker crash or a lost task-enqueue that
       would otherwise leave a draft permanently stuck in `SENDING`.

    Returns a stats dict with both counts.
    """
    stale_count = AutoApplyDraft.objects.filter(
        status=AutoApplyDraft.Status.DRAFTED,
        job__status=Job.Status.CLOSED,
    ).update(status=AutoApplyDraft.Status.STALE, updated_at=timezone.now())

    timeout_seconds = settings.AUTO_APPLY_SENDING_TIMEOUT_SECONDS
    cutoff = timezone.now() - timedelta(seconds=timeout_seconds)
    recovered_count = AutoApplyDraft.objects.filter(
        status=AutoApplyDraft.Status.SENDING,
        updated_at__lt=cutoff,
    ).update(
        status=AutoApplyDraft.Status.FAILED,
        error_message="Submission timed out / recovered from stuck SENDING state.",
        reason_code=AutoApplyDraft.ReasonCode.SENDING_TIMEOUT,
        updated_at=timezone.now(),
    )

    stats = {"stale": stale_count, "recovered_sending": recovered_count}
    logger.info("sweep_stale_auto_apply_drafts: %s", stats)
    return stats
