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

# Salary bands per region (must match forms.py SALARY_BANDS_BY_REGION)
SALARY_BANDS_BY_REGION = {
    "US": [
        ("", "— Select —"),
        ("<50k", "< $50,000"),
        ("50-75k", "$50,000 – $75,000"),
        ("75-100k", "$75,000 – $100,000"),
        ("100-125k", "$100,000 – $125,000"),
        ("125-150k", "$125,000 – $150,000"),
        ("150-175k", "$150,000 – $175,000"),
        ("175-200k", "$175,000 – $200,000"),
        ("200-250k", "$200,000 – $250,000"),
        ("250-300k", "$250,000 – $300,000"),
        ("300-400k", "$300,000 – $400,000"),
        ("400k+", "$400,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "EU": [
        ("", "— Select —"),
        ("<40k", "< €40,000"),
        ("40-55k", "€40,000 – €55,000"),
        ("55-70k", "€55,000 – €70,000"),
        ("70-90k", "€70,000 – €90,000"),
        ("90-120k", "€90,000 – €120,000"),
        ("120-150k", "€120,000 – €150,000"),
        ("150-200k", "€150,000 – €200,000"),
        ("200k+", "€200,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "IN": [
        ("", "— Select —"),
        ("<10L", "< ₹10 LPA"),
        ("10-15L", "₹10 – 15 LPA"),
        ("15-25L", "₹15 – 25 LPA"),
        ("25-35L", "₹25 – 35 LPA"),
        ("35-50L", "₹35 – 50 LPA"),
        ("50-70L", "₹50 – 70 LPA"),
        ("70-100L", "₹70 – 100 LPA"),
        ("100L+", "₹1 Cr+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "UK": [
        ("", "— Select —"),
        ("<35k", "< £35,000"),
        ("35-45k", "£35,000 – £45,000"),
        ("45-60k", "£45,000 – £60,000"),
        ("60-80k", "£60,000 – £80,000"),
        ("80-100k", "£80,000 – £100,000"),
        ("100-130k", "£100,000 – £130,000"),
        ("130-160k", "£130,000 – £160,000"),
        ("160k+", "£160,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "CA": [
        ("", "— Select —"),
        ("<60k", "< C$60,000"),
        ("60-80k", "C$60,000 – C$80,000"),
        ("80-100k", "C$80,000 – C$100,000"),
        ("100-130k", "C$100,000 – C$130,000"),
        ("130-160k", "C$130,000 – C$160,000"),
        ("160-200k", "C$160,000 – C$200,000"),
        ("200k+", "C$200,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "AU": [
        ("", "— Select —"),
        ("<70k", "< A$70,000"),
        ("70-90k", "A$70,000 – A$90,000"),
        ("90-120k", "A$90,000 – A$120,000"),
        ("120-150k", "A$120,000 – A$150,000"),
        ("150-180k", "A$150,000 – A$180,000"),
        ("180-220k", "A$180,000 – A$220,000"),
        ("220k+", "A$220,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "SG": [
        ("", "— Select —"),
        ("<60k", "< S$60,000"),
        ("60-80k", "S$60,000 – S$80,000"),
        ("80-110k", "S$80,000 – S$110,000"),
        ("110-140k", "S$110,000 – S$140,000"),
        ("140-180k", "S$140,000 – S$180,000"),
        ("180-220k", "S$180,000 – S$220,000"),
        ("220k+", "S$220,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
}
DEFAULT_SALARY_BANDS = SALARY_BANDS_BY_REGION["US"]


def _map_country_to_region(country_code: str) -> str:
    """Map ISO country code to salary region key."""
    mapping = {
        "US": "US",
        "CA": "CA",
        "MX": "US",  # NAFTA, often USD bands
        "GB": "UK",
        "IE": "EU",
        "DE": "EU", "FR": "EU", "NL": "EU", "ES": "EU", "IT": "EU", "PL": "EU",
        "SE": "EU", "DK": "EU", "FI": "EU", "NO": "EU", "CH": "EU",
        "IN": "IN",
        "SG": "SG",
        "AU": "AU", "NZ": "AU",
        "JP": "SG",  # closest band
        "KR": "SG",
        "HK": "SG",
    }
    return mapping.get(country_code.upper(), "US")


def _get_salary_band_label(region: str, band_key: str) -> str:
    """Get the display label for a salary band key in a region."""
    bands = SALARY_BANDS_BY_REGION.get(region, DEFAULT_SALARY_BANDS)
    for key, label in bands:
        if key == band_key:
            return label
    return band_key


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

    _carry_forward_confirmed_answers(user, job, answers_payload)

    # -- Salary region resolution from Profile.salary_by_region ---------------
    if profile and profile.salary_by_region:
        # Get job's country from normalized location
        job_country = ""
        if job.target_locations_normalized:
            # target_locations_normalized is a list of {"raw": ..., "country": ...}
            for loc in job.target_locations_normalized:
                if loc.get("country"):
                    job_country = loc["country"]
                    break
        job_region = _map_country_to_region(job_country)
        if job_region in profile.salary_by_region:
            band_key = profile.salary_by_region[job_region]
            label = _get_salary_band_label(job_region, band_key)
            # Find the salary_expectation question in answers_payload and override
            for q_label, entry in answers_payload.items():
                if entry.get("category") == "salary_expectation":
                    entry["value"] = label
                    entry["needs_review"] = False
                    entry["reason"] = "profile_derived_region"
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


def _is_blank(value) -> bool:
    if isinstance(value, (list, tuple)):
        return not any(str(item or "").strip() for item in value)
    return not str(value or "").strip()


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
        if not entry.get("needs_review") or not _is_blank(entry.get("value")):
            continue
        prior_entry = (previous.answers or {}).get(label)
        if not isinstance(prior_entry, dict) or not prior_entry.get("user_confirmed"):
            continue
        if prior_entry.get("field_type") != entry.get("field_type"):
            continue
        prior_value = prior_entry.get("value")
        if _is_blank(prior_value):
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
