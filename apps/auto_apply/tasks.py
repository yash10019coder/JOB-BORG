"""Celery tasks for apps.auto_apply.

Mirrors `apps/jobs/tasks.py`'s conventions: `@shared_task(name="apps.<app>.
<verb_noun>")` naming, per-item `try/except Exception` isolation (`# noqa:
BLE001`) so a failure never crashes the worker, and `transaction.atomic()`
around single-row writes (handled inside `services.drafting._persist_draft`
for the draft create, and inline in `submit_auto_apply_draft` below for the
`JobApplication` upsert + draft status write).
"""
from datetime import timedelta, datetime, timezone
import logging
import time

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone as django_timezone

from apps.applications.models import JobApplication
from apps.jobs.models import Job

from .captcha.base import get_solver
from .email_verification.base import VerificationOutcome
from .email_verification.imap_provider import build_email_code_provider
from .greenhouse_form.client import GreenhouseFormClient
from .greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormVerificationFailed,
)
from .greenhouse_form.field_mapping import schema_from_dict
from .models import AutoApplyDraft
from .services.drafting import draft_for

# Hard kill limit for Celery (D1)
_SUBMIT_HARD_KILL_SECONDS = 900
_SWEEP_SAFETY_MARGIN_SECONDS = 60

logger = logging.getLogger(__name__)

User = get_user_model()


def _submit_budget_seconds() -> float:
    """Compute current submission time budget from settings at call time (D1)."""
    sending_timeout = getattr(settings, "AUTO_APPLY_SENDING_TIMEOUT_SECONDS", 600)
    return max(30.0, float(sending_timeout) - _SWEEP_SAFETY_MARGIN_SECONDS)


def _reason_code_for(exc: GreenhouseFormError) -> str:
    """Map a submission-time exception to `AutoApplyDraft.ReasonCode`."""
    if isinstance(exc, GreenhouseFormChallenged):
        return AutoApplyDraft.ReasonCode.CAPTCHA_CHALLENGED
    if isinstance(exc, GreenhouseFormSchemaMismatch):
        return AutoApplyDraft.ReasonCode.SCHEMA_MISMATCH
    if isinstance(exc, GreenhouseFormVerificationFailed):
        outcome = getattr(exc, "outcome", None)
        outcome_map = {
            VerificationOutcome.NO_INBOX_CREDENTIALS: AutoApplyDraft.ReasonCode.NO_INBOX_CREDENTIALS,
            VerificationOutcome.CODE_TIMEOUT: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_TIMEOUT,
            VerificationOutcome.INBOX_AUTH_FAILED: AutoApplyDraft.ReasonCode.INBOX_AUTH_FAILED,
            VerificationOutcome.INBOX_UNAVAILABLE: AutoApplyDraft.ReasonCode.INBOX_UNAVAILABLE,
            VerificationOutcome.CODE_AMBIGUOUS: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_AMBIGUOUS,
            VerificationOutcome.CODE_REJECTED: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_REJECTED,
        }
        return outcome_map.get(outcome, AutoApplyDraft.ReasonCode.SUBMISSION_FAILED)
    return AutoApplyDraft.ReasonCode.SUBMISSION_FAILED


@shared_task(name="apps.auto_apply.draft_auto_apply")
def draft_auto_apply(user_id, job_id):
    """Draft an auto-apply attempt for `user_id` applying to `job_id`."""
    try:
        user = User.objects.select_related("profile").get(pk=user_id)
        job = Job.objects.get(pk=job_id)
        draft = draft_for(user, job)
    except Exception:  # noqa: BLE001
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
    time_limit=_SUBMIT_HARD_KILL_SECONDS,
)
def submit_auto_apply_draft(draft_id):
    """Drive the real Greenhouse submission for a `SENDING` `AutoApplyDraft`."""
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

    answers = {label: entry.get("value") for label, entry in (draft.answers or {}).items()}
    expected_schema = schema_from_dict(draft.form_schema_snapshot)

    budget = _submit_budget_seconds()
    deadline = time.monotonic() + budget

    provider = build_email_code_provider(draft.user)

    lock_key = f"auto_apply:verification_lock:{draft.user_id}"
    acquired_lock = False
    if provider is not None:
        acquired_lock = bool(cache.add(lock_key, "1", timeout=int(budget)))
        if not acquired_lock:
            logger.warning(
                "submit_auto_apply_draft(draft_id=%s): could not acquire verification lock for user_id=%s.",
                draft_id,
                draft.user_id,
            )
            draft.status = AutoApplyDraft.Status.FAILED
            draft.error_message = "Another application for your account is currently waiting for email verification."
            draft.reason_code = AutoApplyDraft.ReasonCode.VERIFICATION_CODE_AMBIGUOUS
            draft.save(update_fields=["status", "error_message", "reason_code", "updated_at"])
            return draft.pk

    form_client = GreenhouseFormClient(
        debug_artifact_dir=settings.AUTO_APPLY_DEBUG_ARTIFACT_DIR or None
    )
    try:
        try:
            form_client.submit(
                job.source_url,
                answers,
                expected_schema=expected_schema,
                captcha_solver=get_solver(),
                email_code_provider=provider,
                deadline_monotonic=deadline,
            )
        finally:
            if acquired_lock:
                try:
                    cache.delete(lock_key)
                except Exception:
                    pass
    except GreenhouseFormError as exc:
        draft.status = AutoApplyDraft.Status.FAILED
        draft.error_message = str(exc)
        draft.reason_code = _reason_code_for(exc)
        draft.save(update_fields=["status", "error_message", "reason_code", "updated_at"])
        logger.warning(
            "submit_auto_apply_draft(draft_id=%s): submission failed: %s", draft_id, exc
        )
        return draft.pk
    except Exception:  # noqa: BLE001
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
        updated = AutoApplyDraft.objects.filter(
            pk=draft.pk, status=AutoApplyDraft.Status.SENDING
        ).update(
            job_application=job_application,
            status=AutoApplyDraft.Status.APPLIED,
            updated_at=django_timezone.now(),
        )

    if not updated:
        logger.warning(
            "submit_auto_apply_draft(draft_id=%s): submission succeeded and "
            "JobApplication %s -> Applied, but the draft was no longer SENDING.",
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
    """Celery Beat task for staleness & stuck SENDING recovery."""
    stale_count = AutoApplyDraft.objects.filter(
        status=AutoApplyDraft.Status.DRAFTED,
        job__status=Job.Status.CLOSED,
    ).update(status=AutoApplyDraft.Status.STALE, updated_at=django_timezone.now())

    timeout_seconds = settings.AUTO_APPLY_SENDING_TIMEOUT_SECONDS
    cutoff = django_timezone.now() - timedelta(seconds=timeout_seconds)
    recovered_count = AutoApplyDraft.objects.filter(
        status=AutoApplyDraft.Status.SENDING,
        updated_at__lt=cutoff,
    ).update(
        status=AutoApplyDraft.Status.FAILED,
        error_message="Submission timed out / recovered from stuck SENDING state.",
        reason_code=AutoApplyDraft.ReasonCode.SENDING_TIMEOUT,
        updated_at=django_timezone.now(),
    )

    stats = {"stale": stale_count, "recovered_sending": recovered_count}
    logger.info("sweep_stale_auto_apply_drafts: %s", stats)
    return stats
