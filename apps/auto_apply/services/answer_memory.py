"""Remembering what the user answered, so they never type it twice.

Two things happen around a draft:

* **Remember on review** -- when the user edits or confirms an answer in the
  review queue and leaves "remember" ticked, it is saved as one of *their*
  answers (``source=user``) and fills the same question on later
  applications. A legal or commercial (T0/T1) answer is remembered only when
  the user explicitly ticks "Always use this answer", and is then locked.
  Long free-text (``textarea``) answers are about one employer and are never
  remembered. A location-sensitive answer (relocation, on-site) is stored for
  the job's own region unless the user marks it "any region".
* **Observations** -- when the application is sent, what was actually
  submitted for each question is recorded (``AnswerObservation``). That is the
  ground truth the learning loop later counts over; it writes nothing the
  resolver reads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from apps.accounts import question_semantics
from apps.accounts.models import AnswerBank, AnswerObservation
from apps.accounts.regions import REGION_LABELS
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import normalize_question_key, write_answer
from apps.accounts.tiering import Tier, classify_tier
from apps.auto_apply.greenhouse_form.field_mapping import FILE, TEXTAREA
from apps.auto_apply.services.confirmation import is_blank_answer_value

logger = logging.getLogger(__name__)

STANDARD_CATEGORY = "standard"
@dataclass(frozen=True)
class ReviewEdit:
    """One field of a review-queue save: what it was, what it is, what to do."""

    label: str
    value: object
    old: dict
    remember: bool = False
    everywhere: bool = False


def _employer_name(job):
    return getattr(getattr(job, "employer", None), "name", None)


def can_remember(entry):
    """Whether this answer may be remembered at all."""
    if not isinstance(entry, dict):
        return False
    if entry.get("field_type") in (FILE, TEXTAREA):
        return False
    return entry.get("category") != STANDARD_CATEGORY


def remember_ui(label, entry, job):
    """What the review queue offers for this answer, or ``None``."""
    if not can_remember(entry):
        return None
    tier = classify_tier(label)
    region = typed_facts.job_region(job)
    return {
        "checked": tier == Tier.T2_FACTUAL,
        "always": tier in (Tier.T0_LEGAL, Tier.T1_COMMERCIAL),
        "sensitive": question_semantics.is_location_sensitive(label),
        "region_label": REGION_LABELS.get(region, ""),
    }


def _deliberate(edit):
    """Did a human do something to this answer, or is it just re-posted?

    Saving the form re-posts every value, so an unreviewed LLM guess arrives
    looking exactly like an answer the user wrote (and any save marks a value
    ``user_confirmed``). Only a changed or newly filled value, a value the
    user changed on an earlier save (``user_edited``), or an explicit
    confirmation of a held answer is a decision worth remembering.
    """
    old = edit.old
    return (
        old.get("value") != edit.value
        or bool(old.get("needs_confirmation"))
        or bool(old.get("user_edited"))
    )


def _already_saved(profile, label, value, scope, employer):
    """True if the user's own answer for this question is already this value
    (so remembering it again would only add history noise)."""
    return AnswerBank.objects.filter(
        profile=profile,
        question_key=normalize_question_key(label, employer),
        scope_region=scope,
        source=AnswerBank.Source.USER,
        value=value,
    ).exists()


def remember_reviewed_answers(draft, edits):
    """Save the ticked answers as the user's own. Returns how many were saved.

    Never raises into the review save: a failure to remember must not lose the
    user's edit. ``draft.answers`` is the already-saved state, so an answer
    still waiting for confirmation is never remembered.
    """
    saved = 0
    profile = draft.user.profile
    job = draft.job
    region = typed_facts.job_region(job)
    employer = _employer_name(job)
    for edit in edits:
        if not edit.remember or is_blank_answer_value(edit.value):
            continue
        entry = (draft.answers or {}).get(edit.label)
        if not can_remember(entry) or entry.get("needs_confirmation"):
            continue
        if not _deliberate(edit):
            continue
        tier = classify_tier(edit.label)
        sensitive = question_semantics.is_location_sensitive(edit.label)
        scope, everywhere = "", False
        if sensitive:
            if edit.everywhere:
                everywhere = True
            elif region:
                scope = region
            else:
                continue  # region unknown and not marked "any region": do not guess
        stripped = None if entry.get("field_type") == TEXTAREA else employer
        if _already_saved(profile, edit.label, edit.value, scope, stripped):
            continue
        try:
            result = write_answer(
                profile,
                edit.label,
                edit.value,
                AnswerBank.Source.USER,
                options=entry.get("options") or (),
                source_detail={"origin": "review", "draft_id": draft.pk},
                # Ticking "Always use this answer" on a legal/commercial
                # question is the explicit per-question decision; lock it.
                is_locked=tier in (Tier.T0_LEGAL, Tier.T1_COMMERCIAL),
                scope_region=scope,
                applies_everywhere=everywhere,
                employer=employer,
            )
        except Exception:  # pragma: no cover - defensive, see docstring
            logger.exception("remember_reviewed_answers: could not save %r", edit.label)
            continue
        saved += bool(result.applied)
    return saved


def record_observations(draft):
    """Record what is being submitted for each non-standard, non-file answer."""
    job = draft.job
    employer = _employer_name(job)
    region = typed_facts.job_region(job) or ""
    rows = []
    for label, entry in (draft.answers or {}).items():
        if not can_remember_or_observe(entry) or is_blank_answer_value(entry.get("value")):
            continue
        provenance = entry.get("provenance") or {}
        rows.append(
            AnswerObservation(
                profile=draft.user.profile,
                question_key=normalize_question_key(
                    label, None if entry.get("field_type") == TEXTAREA else employer
                ),
                question_text=label,
                value=entry["value"],
                tier=entry.get("tier") or classify_tier(label),
                field_type=entry.get("field_type") or "",
                provenance_source=provenance.get("source", ""),
                provenance_origin=str(provenance.get("origin", ""))[:64],
                was_edited=bool(entry.get("user_confirmed")),
                job_id=draft.job_id,
                draft_id=draft.pk,
                employer_name=employer or "",
                job_region=region,
            )
        )
    AnswerObservation.objects.bulk_create(rows)
    return len(rows)


def can_remember_or_observe(entry):
    """Observations cover every answered non-standard, non-file field,
    including long text (counted by exact wording, never reused across employers)."""
    return (
        isinstance(entry, dict)
        and entry.get("field_type") != FILE
        and entry.get("category") != STANDARD_CATEGORY
    )
