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

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.auto_apply.greenhouse_form.client import GreenhouseFormClient
from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
)
from apps.auto_apply.greenhouse_form.field_mapping import (
    CHECKBOX_GROUP, COMBOBOX_SELECT, FILE, MULTI_SELECT, SINGLE_SELECT, TEXT, FormField, schema_to_dict,
)
from apps.auto_apply.llm import base as llm_base
from apps.auto_apply.llm.base import Question
from apps.auto_apply.models import AutoApplyDraft
from apps.jobs.models import JobSource

from . import answer_resolution
from .confirmation import is_blank_answer_value
from apps.web.salary_bands import (
    DEFAULT_SALARY_BANDS,
    SALARY_BANDS_BY_REGION,
    _get_salary_band_label,
    validate_salary_by_region,
)
from apps.web.regions import region_for_country

logger = logging.getLogger(__name__)

# A COMBOBOX_SELECT sample this small or smaller is enforced as a closed set
# even when options_complete is False (the scroll-convergence signal didn't
# fire). Verified against two real production failures: a 10-entry "Degree"
# dropdown and a 3-entry salary-range dropdown were both flagged incomplete,
# and in both cases the LLM/an ExplicitAnswer's free-text answer (shown the
# sample as a hint, per the Question construction below) still didn't match
# any listed option verbatim and reached a live submission crash instead of
# needs_review. A real closed-choice dropdown this size essentially never
# has more hidden beyond one page; a genuinely large, dynamically-searched
# field (School's thousands of entries, Country) stays unenforced above this
# threshold, where an answer legitimately outside the DOM sample is a real
# possibility worth not blocking.
_SMALL_OPTION_SET_ENFORCE_THRESHOLD = 15


# Rendered-field label -> standard-field key (R4), in priority order (first
# match wins). "full_name"/"name" is anchored to the whole (stripped) label
# so it never shadows "First Name"/"Last Name", which are matched by their
# own, earlier entries first.
_STANDARD_FIELD_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("first_name", re.compile(r"first\s+name", re.I)),
    ("last_name", re.compile(r"last\s+name", re.I)),
    ("full_name", re.compile(r"^(full\s+)?name$", re.I)),
    ("email", re.compile(r"e-?mail(?: address)?", re.I)),
    ("phone", re.compile(r"phone(?: number)?", re.I)),
    ("linkedin", re.compile(r"linkedin(?: (?:url|profile))?", re.I)),
    ("github", re.compile(r"git\s*(?:hub|lab)(?:\s*/\s*git\s*(?:hub|lab))?(?:\s*profile)?(?:\s*url)?", re.I)),
    ("website", re.compile(r"(?:personal\s+)?(?:website|portfolio)(?:\s*url)?", re.I)),
    ("current_company", re.compile(r"current\s+(?:company|employer)", re.I)),
    ("resume", re.compile(r"r[ée]sum[ée](?:\s*/\s*cv)?|cv", re.I)),
)


def _classify_standard_field(label: str) -> str | None:
    """Return the standard-field key a rendered label maps to, or None if
    this is a custom (non-standard) question."""
    stripped = label.strip()
    for key, pattern in _STANDARD_FIELD_PATTERNS:
        if pattern.fullmatch(stripped):
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
    if key == "github":
        return (getattr(profile, "github_url", "") or "").strip()
    if key == "website":
        return (getattr(profile, "portfolio_url", "") or "").strip()
    if key == "current_company":
        return (getattr(profile, "current_employer", "") or "").strip()
    if key == "resume":
        resume = getattr(profile, "resume", None)
        if not resume:
            return ""
        # Store the storage key, not a filesystem path: with remote storage
        # (S3) there is no path, and the worker may not share MEDIA_ROOT with
        # the web process. `submit_auto_apply_draft` copies the file out of
        # `default_storage` into a local temp file just before submitting.
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

    if AutoApplyDraft.objects.filter(
        user=user, job=job, reason_code=AutoApplyDraft.ReasonCode.SUBMISSION_UNCONFIRMED
    ).exists():
        return None

    form_client = form_client or GreenhouseFormClient(
        debug_artifact_dir=settings.AUTO_APPLY_DEBUG_ARTIFACT_DIR or None
    )
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
            reason_code=AutoApplyDraft.ReasonCode.SCHEMA_MISMATCH,
        )
    except GreenhouseFormError as exc:
        return _persist_draft(
            user,
            job,
            status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason=f"Could not load the application form: {exc}",
            reason_code=AutoApplyDraft.ReasonCode.FORM_LOAD_FAILED,
        )

    standard_fields: list[FormField] = []
    custom_fields: list[FormField] = []
    for form_field in schema.fields:
        is_standard = form_field.field_type in (TEXT, FILE) and _classify_standard_field(form_field.label)
        target = standard_fields if is_standard else custom_fields
        target.append(form_field)

    answers_payload: dict[str, dict] = {}
    # Blank *standard* (Profile-derived) required fields are a different
    # problem than an LLM-unanswerable custom question -- they signal an
    # incomplete Profile, which the user fixes on their profile page, not
    # inline per-draft -- so they keep the pre-existing exclude-the-draft
    # behavior. Most custom (LLM-inferred) questions no longer exclude the
    # draft (see the loop below); the one custom-question exception is a
    # required FILE-type field, which also lands here since it can't be
    # answered inline via the review queue either.
    unanswerable_required: list[str] = []

    # -- Standard fields (R4): filled straight from Profile/User. ----------
    for form_field in standard_fields:
        key = _classify_standard_field(form_field.label)
        value = _standard_field_value(key, profile, user)
        if value:
            answers_payload[form_field.label] = {
                "value": value,
                "needs_review": False,
                "required": form_field.required,
                "category": "standard",
                "reason": "profile",
                "field_type": form_field.field_type,
                "options": form_field.options,
                "options_complete": form_field.options_complete,
            }
        elif form_field.required:
            unanswerable_required.append(form_field.label)

    # -- Custom fields (R5): explicit answer, then LLM inference. -----------
    # FILE-type custom questions (e.g. "Upload your portfolio") are never
    # sent to the LLM -- it has no way to produce a real answer for a file
    # upload. A required one excludes the draft, same as any other
    # unanswerable required field; an optional one still needs a blank
    # placeholder entry in answers_payload (not just silent omission) so
    # the review queue renders it and `_blocking_required_fields` has a
    # consistent shape to check, matching every other optional field type.
    for f in custom_fields:
        if f.field_type != FILE:
            continue
        if f.required:
            unanswerable_required.append(f.label)
        else:
            answers_payload[f.label] = {
                "value": "",
                "needs_review": False,
                "required": False,
                "category": "custom",
                "reason": "file_field",
                "field_type": f.field_type,
            }
    custom_fields = [f for f in custom_fields if f.field_type != FILE]
    questions = [
        Question(
            id=f.label, text=f.label, field_type=f.field_type,
            # Always show the LLM whatever sample of options was discovered
            # -- even an incomplete COMBOBOX_SELECT one -- so it recognizes
            # a constrained-choice question and answers with a real option
            # instead of free-text prose (verified against a production
            # failure: a Yes/No-style combobox with options_complete=False
            # got a full-sentence LLM answer that matched no live option).
            # `options_enforced=False` keeps `_enforce_option_constraint`
            # from rejecting a legitimate answer the incomplete sample
            # simply didn't happen to include.
            options=f.options,
            options_enforced=(
                f.field_type != COMBOBOX_SELECT
                or f.options_complete
                or len(f.options) <= _SMALL_OPTION_SET_ENFORCE_THRESHOLD
            ),
        )
        for f in custom_fields
    ]
    resolved = answer_resolution.resolve_field_answers(
        user, questions, resume_text, profile, llm_client
    )
    fields_by_label = {f.label: f for f in custom_fields}

    for resolved_answer in resolved:
        form_field = fields_by_label[resolved_answer.question_id]
        if not resolved_answer.answer:
            # Falsy covers `None` (hard-excluded / LLM-infra-failure /
            # missing-response) and `""` (e.g. `insufficient_evidence=True`
            # with no answer text) -- neither is a real answer to submit.
            # None of these are a judgment that the *application* is
            # unanswerable, only that the LLM couldn't answer this one
            # question -- so it never excludes the whole draft. Leave a
            # blank, needs_review placeholder (required or not) for a human
            # to fill in via the review queue (`edit_auto_apply_draft`)
            # before sending; `send_auto_apply_draft` blocks sending while a
            # required placeholder is still blank (see apps/web/views.py).
            #
            # Exception: a required FILE-type question (e.g. "upload your
            # portfolio"). `edit_auto_apply_draft` deliberately excludes
            # FILE-type answers from editing (a user-supplied string there
            # would flow straight into Playwright's set_input_files() at
            # send time -- see that view's docstring), so a blank FILE
            # placeholder here would be a permanent, unfillable blocker
            # rather than something a human can actually resolve inline.
            # This one case keeps the pre-existing exclude-the-draft
            # behavior instead.
            if form_field.field_type == FILE and form_field.required:
                unanswerable_required.append(form_field.label)
                continue
            answers_payload[form_field.label] = {
                "value": "",
                "needs_review": True,
                "required": form_field.required,
                "category": resolved_answer.category,
                "reason": resolved_answer.reason,
                "field_type": form_field.field_type,
                "options": form_field.options,
                "options_complete": form_field.options_complete,
            }
            continue
        value = resolved_answer.answer
        invalid_option = False
        if form_field.field_type in (SINGLE_SELECT, MULTI_SELECT, CHECKBOX_GROUP):
            values = value if isinstance(value, (list, tuple)) else [value]
            invalid_option = (
                not values
                or (form_field.field_type == SINGLE_SELECT and len(values) != 1)
                or any(v not in form_field.options for v in values)
            )
        answers_payload[form_field.label] = {
            "value": "" if invalid_option else value,
            "needs_review": invalid_option or resolved_answer.needs_review,
            "required": form_field.required,
            "category": resolved_answer.category,
            "reason": resolved_answer.reason,
            "field_type": form_field.field_type,
            "options": form_field.options,
                "options_complete": form_field.options_complete,
        }
        if resolved_answer.provenance is not None:
            # Only AnswerBank-sourced answers carry these, so drafts built
            # from the legacy/LLM paths keep their exact prior shape.
            entry = answers_payload[form_field.label]
            entry["needs_confirmation"] = resolved_answer.needs_confirmation
            entry["provenance"] = resolved_answer.provenance
            entry["tier"] = resolved_answer.tier

    _carry_forward_confirmed_answers(user, job, answers_payload)

    # -- Salary region resolution from Profile.salary_by_region ---------------
    if profile and profile.salary_by_region:
        # Validate the stored salary_by_region first
        cleaned_salary_by_region = validate_salary_by_region(profile.salary_by_region)

        # Get job's country from normalized location
        job_country = ""
        if job.target_locations_normalized:
            # target_locations_normalized is a list of {"raw": ..., "country": ...}
            for loc in job.target_locations_normalized:
                if loc.get("country"):
                    job_country = loc["country"]
                    break
        job_region = region_for_country(job_country)

        if job_region and job_region in cleaned_salary_by_region:
            band_key = cleaned_salary_by_region[job_region]
            label = _get_salary_band_label(job_region, band_key)

            # Find the salary_expectation question in answers_payload and override
            for q_label, entry in answers_payload.items():
                if entry.get("category") == "salary_expectation":
                    # Do not override user-confirmed answers
                    if entry.get("user_confirmed"):
                        continue
                    # For option-bearing fields, validate the band key against form options
                    field_options = entry.get("options") or []
                    if field_options and label not in field_options:
                        # Band key not in current form's options - keep existing answer with needs_review
                        entry["needs_review"] = True
                        entry["reason"] = "profile_derived_region_invalid_option"
                        continue
                    # Valid override
                    entry["value"] = label
                    entry["needs_review"] = False
                    entry["reason"] = "profile_derived_region"
                    if "provenance" in entry:
                        # The value now comes from the user's own profile, not
                        # the AnswerBank row that was resolved first: drop that
                        # row's hold and attribution so the user isn't asked to
                        # confirm their own band and the submit snapshot
                        # records the real source.
                        entry["needs_confirmation"] = False
                        entry["provenance"] = {
                            "origin": "profile.salary_by_region",
                            "source": "user",
                            "locked": False,
                            "confidence": 1.0,
                            "detail": {"region": job_region},
                        }
                    break

    if unanswerable_required:
        # Only blank *standard* (Profile-derived) required fields land here
        # now -- an incomplete Profile, not an LLM judgment call. Custom
        # questions the LLM couldn't answer no longer exclude the draft;
        # they become blank needs_review placeholders in answers_payload
        # above instead (see the loop above and Status.EXCLUDED's docstring
        # on AutoApplyDraft).
        reason = "Required question(s) could not be answered: " + "; ".join(
            unanswerable_required
        )
        return _persist_draft(
            user,
            job,
            status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason=reason,
            reason_code=AutoApplyDraft.ReasonCode.UNANSWERABLE_REQUIRED,
        )

    return _persist_draft(
        user,
        job,
        status=AutoApplyDraft.Status.DRAFTED,
        answers=answers_payload,
        form_schema_snapshot=schema_to_dict(schema),
    )


def _carry_forward_confirmed_answers(user, job, answers_payload) -> None:
    """Reuse a previously user-confirmed answer for a still-blank,
    `needs_review` field from the most recent earlier draft for this
    (user, job) pair -- mutates `answers_payload` in place.

    Most relevant for DEMOGRAPHIC/EEO questions (gender, veteran status,
    disability, race/ethnicity, etc.): `answer_resolution` always hard-
    excludes these from LLM inference (see
    `apps.auto_apply.llm.categories.HARD_EXCLUDED_CATEGORIES`), so every
    *fresh* draft -- including a Retry after a FAILED send -- would
    otherwise land these blank again even though the user already answered
    them by hand via `edit_auto_apply_draft` on a prior attempt for this
    exact job, forcing a re-answer of the same voluntary self-ID questions
    on every retry.

    Only reuses an answer explicitly marked `user_confirmed` on the prior
    draft -- set only by `edit_auto_apply_draft`'s save path
    (apps/web/views.py), never by drafting itself -- for the exact same
    question label and field type, and -- for an option-bearing field --
    only when that value is still a valid option on the schema just
    re-inspected for *this* draft, since Greenhouse's rendered options can
    drift between attempts.

    `needs_review=False` alone is NOT used as the confirmation signal: a
    confident LLM answer also gets `needs_review=False` (see
    `answer_resolution`/`llm/base.py`) with no human ever having looked at
    it, so carrying that forward on a retry would silently present an
    LLM guess as something the user vouched for (CodeRabbit finding on
    PR #99).
    """
    previous = (
        AutoApplyDraft.objects.filter(user=user, job=job)
        .exclude(answers={})
        .order_by("-updated_at")
        .first()
    )
    if previous is None:
        return

    for label, entry in answers_payload.items():
        if not entry.get("needs_review") or not is_blank_answer_value(entry.get("value")):
            continue
        prior_entry = (previous.answers or {}).get(label)
        if not isinstance(prior_entry, dict) or not prior_entry.get("user_confirmed"):
            continue
        if prior_entry.get("field_type") != entry.get("field_type"):
            continue
        prior_value = prior_entry.get("value")
        if is_blank_answer_value(prior_value):
            continue
        # Re-validate against the *current* schema's options before
        # reusing a confirmed value. SINGLE_SELECT/MULTI_SELECT/
        # CHECKBOX_GROUP always carry a complete option set (see
        # field_mapping._OPTION_BEARING_TYPES), so always enforced here.
        # COMBOBOX_SELECT only carries a complete sample sometimes
        # (`options_complete`); enforcing against an incomplete sample
        # would wrongly reject a value that's legitimately outside the
        # DOM's partial render (CodeRabbit finding on PR #99).
        option_bearing = entry.get("field_type") in (SINGLE_SELECT, MULTI_SELECT, CHECKBOX_GROUP) or (
            entry.get("field_type") == COMBOBOX_SELECT and entry.get("options_complete")
        )
        if option_bearing and entry.get("options"):
            values = prior_value if isinstance(prior_value, (list, tuple)) else [prior_value]
            if any(v not in entry["options"] for v in values):
                continue
        entry["value"] = prior_value
        entry["needs_review"] = False
        entry["reason"] = "carried_forward_from_previous_draft"


def _persist_draft(
    user,
    job,
    *,
    status,
    answers=None,
    exclusion_reason=None,
    reason_code=None,
    form_schema_snapshot=None,
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
                reason_code=reason_code,
                form_schema_snapshot=form_schema_snapshot,
            )
    except IntegrityError:
        logger.info(
            "draft_for(user=%s, job=%s): an active draft already exists; "
            "treating this trigger as a no-op.",
            getattr(user, "pk", user),
            getattr(job, "pk", job),
        )
        return None
