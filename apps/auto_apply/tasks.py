"""Celery tasks for apps.auto_apply.

Mirrors `apps/jobs/tasks.py`'s conventions: `@shared_task(name="apps.<app>.
<verb_noun>")` naming, per-item `try/except Exception` isolation (`# noqa:
BLE001`) so a failure never crashes the worker, and `transaction.atomic()`
around single-row writes (handled inside `services.drafting._persist_draft`
for the draft create, and inline in `submit_auto_apply_draft` below for the
`JobApplication` upsert + draft status write).
"""
from contextlib import contextmanager, closing
from copy import copy
from datetime import timedelta, datetime, timezone
import gzip
import logging
import math
import os
import pickle
import tempfile
import time
import uuid

from botocore.config import Config
from boto3.s3.transfer import S3Transfer
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone as django_timezone
from storages.backends.s3 import S3Storage

from apps.applications.models import JobApplication
from apps.jobs.models import Job

from .captcha.base import get_solver
from .checks import _SUBMIT_HARD_KILL_SECONDS
from .email_verification.base import VerificationOutcome
from .email_verification.imap_provider import build_email_code_provider
from .greenhouse_form.client import GreenhouseFormClient
from .greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormSubmissionUnconfirmed,
    GreenhouseFormVerificationFailed,
)
from .greenhouse_form.field_mapping import FILE, schema_from_dict
from .models import AutoApplyDraft
from .services.drafting import draft_for

# Hard kill limit for Celery (D1). Single source of truth lives in
# apps.auto_apply.checks (imported above) since it's a lightweight module
# with no heavy transitive imports -- checks.py's own system check enforces
# this stays strictly greater than AUTO_APPLY_SENDING_TIMEOUT_SECONDS.
_SWEEP_SAFETY_MARGIN_SECONDS = 60

# Backstop for draft_auto_apply: Playwright inspection is independently
# bounded (see client.py's goto/settle/combobox timeouts), and the LLM call
# is bounded by AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS -- this is a pure
# kill switch in case both bounds are somehow bypassed, not the operative
# budget. Confirmed live: with no limit at all, an unbounded LLM client
# timeout let a single draft_auto_apply invocation hang indefinitely,
# occupying a worker slot with no AutoApplyDraft ever created.
_DRAFT_HARD_KILL_SECONDS = 180

logger = logging.getLogger(__name__)

User = get_user_model()


def _submit_budget_seconds() -> float:
    """Compute current submission time budget from settings at call time (D1)."""
    sending_timeout = getattr(settings, "AUTO_APPLY_SENDING_TIMEOUT_SECONDS", 600)
    return max(30.0, float(sending_timeout) - _SWEEP_SAFETY_MARGIN_SECONDS)


def _remaining_file_budget(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise GreenhouseFormError("Submission deadline expired while preparing file uploads.")
    return remaining


@contextmanager
def _file_answer_storage(deadline_monotonic: float):
    storage = storages["default"]
    if not isinstance(storage, S3Storage):
        yield storage
        return

    # S3Storage's copy protocol resets cached connections. Keep timeout changes
    # local to this submission, without mutating the shared default backend.
    storage = copy(storage)
    remaining = _remaining_file_budget(deadline_monotonic)
    storage.client_config = storage.client_config.merge(Config(
        connect_timeout=min(storage.client_config.connect_timeout, remaining),
        read_timeout=min(storage.client_config.read_timeout, remaining),
        retries={"total_max_attempts": 1},
    ))
    with closing(storage.connection.meta.client):
        yield storage


@contextmanager
def _file_answer_stream(storage, source):
    if not isinstance(storage, S3Storage):
        yield source, source
        return

    # S3File.read() first downloads the ENTIRE object into a spool. Stream the
    # object directly so each network read can honor the remaining budget.
    params = {
        key: value for key, value in storage.get_object_parameters(source.name).items()
        if key in S3Transfer.ALLOWED_DOWNLOAD_ARGS
    }
    response = source.obj.get(**params)
    with closing(response["Body"]) as body:
        if storage.gzip and response.get("ContentEncoding") == "gzip":
            with gzip.GzipFile(fileobj=body, mode="rb") as stream:
                yield stream, body
        else:
            yield body, body


def _materialize_file_answers(
    entries: dict, answers: dict, temp_files: list[str], *, deadline_monotonic: float,
) -> None:
    """Resolve storage keys to local uploads, retaining legacy absolute paths.

    Missing keys retain their existing fallback. Register partial temp files
    immediately so the caller also cleans up failed or timed-out copies.
    """
    for label, entry in entries.items():
        value = answers.get(label)
        if entry.get("field_type") != FILE or not value:
            continue
        value = str(value)
        # Absolute paths are legacy answers, not storage keys. In particular,
        # FileSystemStorage rejects paths outside MEDIA_ROOT in exists().
        if os.path.isabs(value) and os.path.isfile(value):
            continue
        with _file_answer_storage(deadline_monotonic) as storage:
            _remaining_file_budget(deadline_monotonic)
            if not storage.exists(value):
                continue
            _remaining_file_budget(deadline_monotonic)
            with storage.open(value, "rb") as source:
                _remaining_file_budget(deadline_monotonic)
                with _file_answer_stream(storage, source) as streams:
                    stream, timeout_source = streams
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(value)[1]) as target:
                        temp_files.append(target.name)
                        while True:
                            remaining = _remaining_file_budget(deadline_monotonic)
                            set_timeout = getattr(timeout_source, "set_socket_timeout", None)
                            if callable(set_timeout):
                                set_timeout(remaining)
                            chunk = stream.read(64 * 1024)
                            _remaining_file_budget(deadline_monotonic)
                            if not chunk:
                                break
                            target.write(chunk)
        answers[label] = temp_files[-1]


def _cleanup_temp_files(temp_files: list[str]) -> None:
    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    temp_files.clear()


def _release_verification_lock(lock_key: str, token: str) -> None:
    """Atomically release only our lease on the configured cache backend."""
    backend = caches["default"]
    key = backend.make_and_validate_key(lock_key)
    if isinstance(backend, RedisCache):
        client = backend._cache.get_client(key, write=True)
        client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1,
            key,
            backend._cache._serializer.dumps(token),
        )
    elif isinstance(backend, LocMemCache):
        # Tests use LocMemCache; comparison and deletion share its mutex.
        with backend._lock:
            if not backend._has_expired(key) and pickle.loads(backend._cache[key]) == token:
                backend._delete(key)
    else:
        # An unsupported backend must expire the lease rather than risk
        # deleting a newer owner's lock with a non-atomic get/delete pair.
        logger.warning("Cannot atomically release verification lock on %s", type(backend).__name__)


def _reason_code_for(exc: GreenhouseFormError) -> str:
    """Map a submission-time exception to `AutoApplyDraft.ReasonCode`.

    Used instead of substring-matching `str(exc)` later in the UI layer
    (`apps/web/views.py`) -- the exception type is already the structured
    signal; discarding it into free text and re-deriving it from prose
    elsewhere would let the two drift out of sync silently.
    """
    if isinstance(exc, GreenhouseFormSubmissionUnconfirmed):
        return AutoApplyDraft.ReasonCode.SUBMISSION_UNCONFIRMED
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


@shared_task(name="apps.auto_apply.draft_auto_apply", time_limit=_DRAFT_HARD_KILL_SECONDS)
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
    bind=True,
    max_retries=None,
    time_limit=_SUBMIT_HARD_KILL_SECONDS,
)
def submit_auto_apply_draft(self, draft_id):
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
    temp_files: list[str] = []
    expected_schema = schema_from_dict(draft.form_schema_snapshot)

    budget = _submit_budget_seconds()
    deadline = time.monotonic() + budget

    provider = build_email_code_provider(draft.user)

    lock_key = f"auto_apply:verification_lock:{draft.user_id}"
    # Owner token: the lease must outlive the whole run (a submission can
    # legitimately take the full budget, and a shorter lease would let a second
    # submission poll the same inbox concurrently), and release must only
    # delete OUR lock -- never one a later task acquired after ours expired.
    lock_token = uuid.uuid4().hex
    acquired_lock = False
    if provider is not None:
        # Round up for Redis's integer TTL, cover the entire submission
        # budget, and add a margin so the lease can't expire right at the
        # edge of a run that took the full budget.
        acquired_lock = bool(cache.add(lock_key, lock_token, timeout=math.ceil(budget) + 30))
        if not acquired_lock:
            logger.warning(
                "submit_auto_apply_draft(draft_id=%s): could not acquire verification lock for user_id=%s.",
                draft_id,
                draft.user_id,
            )
            raise self.retry(countdown=10)

    try:
        form_client = GreenhouseFormClient(
            debug_artifact_dir=settings.AUTO_APPLY_DEBUG_ARTIFACT_DIR or None
        )
        try:
            _materialize_file_answers(
                draft.answers or {}, answers, temp_files, deadline_monotonic=deadline,
            )
            form_client.submit(
                job.source_url,
                answers,
                expected_schema=expected_schema,
                captcha_solver=get_solver(),
                email_code_provider=provider,
                deadline_monotonic=deadline,
            )
        finally:
            _cleanup_temp_files(temp_files)
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
    finally:
        if acquired_lock:
            try:
                _release_verification_lock(lock_key, lock_token)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "submit_auto_apply_draft(draft_id=%s): failed to release verification lock.",
                    draft_id,
                )

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
