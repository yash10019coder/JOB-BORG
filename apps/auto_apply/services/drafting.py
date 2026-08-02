"""Drafting orchestration (U6): wires U1 (Profile fields), U2 (models), U3
(`GreenhouseFormClient`), and U4 (LLM answer inference, via
`answer_resolution`) together to turn a (user, job) pair into either a
`status=DRAFTED` `AutoApplyDraft` (R7) or a `status=EXCLUDED` one with an
`exclusion_reason` set (R6) -- always a persisted row, never an ephemeral,
unpersisted result, so the reason stays visible in the review queue (U8)
after the triggering request/task session ends.

Navigates directly via `Job.source_url` (the exact Greenhouse
`absolute_url` captured at ingestion time -- see
`apps/jobs/ingestion/normalizers.py`), not a `Job` -> `Employer` ->
`JobSource` join: `source_url` is already the exact, verbatim application
URL, and reconstructing one from `board_token` would be both unnecessary
and riskier for boards with custom domains/slugs (an `Employer` can also
have `JobSource`s across multiple ATSes, making that join ambiguous).
"""
from __future__ import annotations

import logging
import re

from django.db import IntegrityError, transaction

from apps.auto_apply.greenhouse_form.client import GreenhouseFormClient
from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
)
from apps.auto_apply.greenhouse_form.field_mapping import FormField
from apps.auto_apply.llm import base as llm_base
from apps.auto_apply.llm.base import Question
from apps.auto_apply.models import AutoApplyDraft
from apps.jobs.models import JobSource

from . import answer_resolution

logger = logging.getLogger(__name__)

# Rendered-field label -> standard-field key (R4), in priority order (first
# match wins). "full_name"/"name" is anchored to the whole (stripped) label
# so it never shadows "First Name"/"Last Name", which are matched by their
# own, earlier entries first.
_STANDARD_FIELD_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("first_name", re.compile(r"first\s*name", re.I)),
    ("last_name", re.compile(r"last\s*name", re.I)),
    ("full_name", re.compile(r"^(full\s*)?name$", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone", re.I)),
    ("linkedin", re.compile(r"linkedin", re.I)),
    ("resume", re.compile(r"r[ée]sum[ée]|\bcv\b", re.I)),
)


def _classify_standard_field(label: str) -> str | None:
    """Return the standard-field key a rendered label maps to, or None if
    this is a custom (non-standard) question."""
    stripped = label.strip()
    for key, pattern in _STANDARD_FIELD_PATTERNS:
        if pattern.search(stripped):
            return key
    return None


def _standard_field_value(key: str, profile, user) -> str:
    """Best-effort value for a standard field from Profile/User data (R4).

    Returns "" for anything unavailable (no profile, blank field, no resume
    uploaded yet) -- callers treat a blank value on a *required* standard
    field the same as any other unanswerable required field (R6), rather
    than crashing or fabricating one.
    """
    full_name = (getattr(profile, "full_name", "") or "").strip()

    if key == "first_name":
        parts = full_name.split()
        return parts[0] if parts else ""
    if key == "last_name":
        parts = full_name.split()
        return parts[-1] if len(parts) > 1 else ""
    if key == "full_name":
        return full_name
    if key == "email":
        return getattr(user, "email", "") or ""
    if key == "phone":
        return (getattr(profile, "phone", "") or "").strip()
    if key == "linkedin":
        return (getattr(profile, "linkedin_url", "") or "").strip()
    if key == "resume":
        resume = getattr(profile, "resume", None)
        if not resume:
            return ""
        try:
            return resume.path
        except (ValueError, NotImplementedError):
            # No file associated with the FieldFile, or a storage backend
            # with no filesystem path (e.g. remote object storage) -- fall
            # back to the stored name rather than raising.
            return resume.name or ""
    return ""


def draft_for(user, job, *, form_client=None, llm_client=None) -> AutoApplyDraft | None:
    """Produce (or reuse) an `AutoApplyDraft` for `user` applying to `job`.

    Returns the created `AutoApplyDraft` (status `DRAFTED` or `EXCLUDED`),
    or `None` if a concurrent duplicate trigger was detected and treated as
    a no-op -- an active (`DRAFTED`/`SENDING`) draft for this (user, job)
    already exists, so the conditional-unique-constraint `IntegrityError`
    from `_persist_draft` is swallowed rather than surfaced as a failure.

    Raises:
        ValueError: `job.source_ats` isn't Greenhouse (R1). Callers (the
            trigger view/task, U8) are only ever expected to call this for
            jobs they've already confirmed are Greenhouse-sourced -- this
            is a precondition violation, not a per-job drafting outcome, so
            it is intentionally not modeled as an `EXCLUDED` row.
    """
    if job.source_ats != JobSource.ATS.GREENHOUSE:
        raise ValueError(
            f"draft_for() only supports Greenhouse-sourced jobs (job {job.pk} "
            f"has source_ats={job.source_ats!r})."
        )

    form_client = form_client or GreenhouseFormClient()
    llm_client = llm_client or llm_base.get_client()
    profile = getattr(user, "profile", None)
    resume_text = getattr(profile, "resume_text", "") or ""

    try:
        schema = form_client.inspect(job.source_url)
    except GreenhouseFormSchemaMismatch as exc:
        return _persist_draft(
            user,
            job,
            status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason=f"Application form has an unsupported field: {exc}",
        )
    except GreenhouseFormError as exc:
        return _persist_draft(
            user,
            job,
            status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason=f"Could not load the application form: {exc}",
        )

    standard_fields: list[FormField] = []
    custom_fields: list[FormField] = []
    for form_field in schema.fields:
        target = standard_fields if _classify_standard_field(form_field.label) else custom_fields
        target.append(form_field)

    answers_payload: dict[str, dict] = {}
    unanswerable_required: list[str] = []

    # -- Standard fields (R4): filled straight from Profile/User. ----------
    for form_field in standard_fields:
        key = _classify_standard_field(form_field.label)
        value = _standard_field_value(key, profile, user)
        if value:
            answers_payload[form_field.label] = {
                "value": value,
                "needs_review": False,
                "category": "standard",
                "reason": "profile",
            }
        elif form_field.required:
            unanswerable_required.append(form_field.label)

    # -- Custom fields (R5): explicit answer, then LLM inference. -----------
    questions = [Question(id=f.label, text=f.label) for f in custom_fields]
    resolved = answer_resolution.resolve_field_answers(
        user, questions, resume_text, profile, llm_client
    )
    fields_by_label = {f.label: f for f in custom_fields}

    for resolved_answer in resolved:
        form_field = fields_by_label[resolved_answer.question_id]
        if not resolved_answer.answer:
            # Falsy covers both `None` (hard-excluded / LLM-call-failed /
            # missing-response) and `""` (e.g. `insufficient_evidence=True`
            # with no answer text) -- neither is a real answer to submit.
            if form_field.required:
                unanswerable_required.append(form_field.label)
            continue
        answers_payload[form_field.label] = {
            "value": resolved_answer.answer,
            "needs_review": resolved_answer.needs_review,
            "category": resolved_answer.category,
            "reason": resolved_answer.reason,
        }

    if unanswerable_required:
        # R6: any required field ending up unanswerable excludes the job
        # for this user -- as a persisted, revisitable row, not a crash or
        # a silent drop.
        reason = "Required question(s) could not be answered: " + "; ".join(
            unanswerable_required
        )
        return _persist_draft(
            user, job, status=AutoApplyDraft.Status.EXCLUDED, exclusion_reason=reason
        )

    return _persist_draft(
        user, job, status=AutoApplyDraft.Status.DRAFTED, answers=answers_payload
    )


def _persist_draft(
    user, job, *, status, answers=None, exclusion_reason=None
) -> AutoApplyDraft | None:
    """Create the `AutoApplyDraft` row, treating a violation of the
    conditional unique constraint (`uniq_autoapplydraft_user_job_active`)
    as a benign concurrent-trigger no-op rather than a task failure -- two
    rapid "Auto-apply" clicks for the same job can enqueue two
    `draft_auto_apply` invocations before either completes.
    """
    try:
        with transaction.atomic():
            return AutoApplyDraft.objects.create(
                user=user,
                job=job,
                status=status,
                answers=answers or {},
                exclusion_reason=exclusion_reason,
            )
    except IntegrityError:
        logger.info(
            "draft_for(user=%s, job=%s): an active draft already exists; "
            "treating this trigger as a no-op.",
            getattr(user, "pk", user),
            getattr(job, "pk", job),
        )
        return None
