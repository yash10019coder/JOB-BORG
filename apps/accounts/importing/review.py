"""The review step: what the user sees for an import, and applying their decisions.

``build_review`` turns a ready job's payload into rows (Current vs Imported) with
safe defaults; ``apply_review`` validates the posted decisions server-side again
and writes the accepted fields and entries in one transaction, then closes the
job (rules R1-R8, V4).

* A field already set by the user, learned or locked is shown read-only as
  *Kept* and can never be overwritten (R2, R5).
* A value the user edited in the review is stored as theirs; one accepted
  unchanged is stored as imported (R3).
* Nothing is deleted, and either everything accepted is written or nothing is (R4).
"""
from dataclasses import dataclass, field
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import ImportJob, ProfileSkill, ResumeEntry
from apps.accounts.services import profile_fields, profile_skills
from apps.accounts.services.profile_fields import ImportApplyError, ImportState
from apps.accounts.services.profile_skills import SkillValueError
from apps.accounts.services.resume_facts import (
    EntryValueError,
    apply_entries,
    find_existing,
    natural_key,
    parse_month,
)

FIELD_ORDER = (
    "full_name", "headline", "phone", "location_city", "location_country", "current_employer",
    "linkedin_url", "github_url", "portfolio_url", "target_titles", "target_tags",
)
FIELD_LABELS = {
    "full_name": "Full name", "headline": "Headline", "phone": "Phone",
    "location_city": "City", "location_country": "Country (code)", "current_employer": "Current employer",
    "linkedin_url": "LinkedIn", "github_url": "GitHub", "portfolio_url": "Portfolio / website",
    "target_titles": "Job titles you are looking for", "target_tags": "Skills for job matching",
}
LIST_FIELDS = {"target_tags": ",", "target_titles": "\n"}
KEPT_LABELS = {
    ImportState.USER: "Kept (you set this)",
    ImportState.LOCKED: "Kept (locked)",
    ImportState.LEARNED: "Kept (learned from your applications)",
}


class ReviewUnavailable(Exception):
    """The job is not ready for review (already applied, discarded, expired...)."""


class ReviewError(Exception):
    """Posted values failed validation; nothing was written."""

    def __init__(self, field_errors=None, entry_errors=None, skill_errors=None):
        super().__init__("invalid review")
        self.field_errors = field_errors or {}
        self.entry_errors = entry_errors or {}
        self.skill_errors = skill_errors or {}


@dataclass
class FieldRow:
    key: str
    label: str
    state: str
    current: str
    imported: str  # the value as it appears in the input
    original: str  # the proposal, for edit detection
    snippet: str
    provisional: bool
    extractor: str
    accept: bool
    kept: bool = False
    unchanged: bool = False
    kept_label: str = ""
    error: str = ""
    is_list: bool = False
    multiline: bool = False


@dataclass
class EntryRow:
    index: int
    kind: str
    title: str
    organization: str
    start: str
    end: str
    is_current: bool
    skills: str
    precision: str
    snippet: str
    needs_check: bool
    layout_inferred: bool
    rule_fallback: bool
    existing: str  # new | update | unchanged | kept
    accept: bool
    original: dict = field(default_factory=dict)
    error: str = ""
    description: str = ""
    url: str = ""


@dataclass
class SkillRow:
    index: int
    name: str
    proof: str
    existing: str  # new | update | unchanged | removed
    accept: bool
    original: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class Review:
    fields: list
    entries: list
    truncated: bool = False
    skills: list = field(default_factory=list)
    mode: str = ""  # GitHub imports: full | light | partial


@dataclass
class Outcome:
    applied: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    entries_created: int = 0
    entries_updated: int = 0
    entries_kept: int = 0
    skills_created: int = 0
    skills_updated: int = 0


def _to_input(key, value):
    if key == "target_tags":
        return ", ".join(value)
    if key == "target_titles":
        return "\n".join(value)
    return "" if value is None else str(value)


def _from_input(key, raw):
    if key == "target_tags":
        return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
    if key == "target_titles":
        return [part.strip() for part in raw.splitlines() if part.strip()]
    return raw.strip()


def _norm(value):
    if isinstance(value, list):
        return [str(v).strip().casefold() for v in value]
    return " ".join(str(value or "").split()).casefold()


def _month(value):
    return parse_month(value)


def _skills_text(skills):
    return ", ".join(skills or [])


def _parse_skills(raw):
    return [part.strip() for part in (raw or "").replace("\n", ",").split(",") if part.strip()]


def _existing_state(profile, data):
    """Compare an entry proposal with what the profile already stores."""
    try:
        start = _month(data.get("start"))
    except EntryValueError:
        start = None
    key = natural_key(data["kind"], data.get("organization", ""), data.get("title", ""), start)
    stored = find_existing(profile, {"kind": data["kind"], "url": data.get("url") or ""}, key)
    if stored is None:
        return "new"
    if stored.source == ResumeEntry.Source.USER:
        return "kept"
    same = (
        stored.title == data.get("title") and stored.organization == data.get("organization", "")
        and stored.description == (data.get("description") or "") and stored.url == (data.get("url") or "")
        and stored.start_date == start and stored.is_current == bool(data.get("is_current"))
        and (stored.end_date.strftime("%Y-%m") if stored.end_date else None) == data.get("end")
        and list(stored.skills or []) == list(data.get("skills", []))
    )
    return "unchanged" if same else "update"


def build_review(job, posted=None):
    """The rows for the review page. With ``posted`` (a failed apply), the user's
    input and decisions are kept so errors can be shown next to them."""
    profile = job.profile
    payload = job.payload or {}
    rows = []
    for key in FIELD_ORDER:
        proposal = (payload.get("fields") or {}).get(key)
        if not proposal:
            continue
        state = profile_fields.import_state(profile, key)
        current_value = getattr(profile, key)
        original = _to_input(key, proposal["value"])
        kept = state not in ImportState.REPLACEABLE
        unchanged = state == ImportState.IMPORTED and _norm(current_value) == _norm(proposal["value"])
        accept = not kept and not unchanged and not proposal.get("provisional") and state in ImportState.REPLACEABLE
        imported = original
        if posted is not None:
            accept = posted.get(f"decision__{key}") == "accept"
            imported = posted.get(f"value__{key}", original)
        rows.append(FieldRow(
            key=key, label=FIELD_LABELS[key], state=state, current=_to_input(key, current_value),
            imported=imported, original=original, snippet=proposal.get("snippet", ""),
            provisional=bool(proposal.get("provisional")), extractor=proposal.get("extractor", "rule"),
            accept=accept, kept=kept, unchanged=unchanged, kept_label=KEPT_LABELS.get(state, ""),
            is_list=key in LIST_FIELDS, multiline=key == "target_titles",
        ))
    entries = []
    for index, data in enumerate(payload.get("entries") or []):
        existing = _existing_state(profile, data)
        row = EntryRow(
            index=index, kind=data["kind"], title=data.get("title", ""), organization=data.get("organization", ""),
            start=data.get("start") or "", end=data.get("end") or "", is_current=bool(data.get("is_current")),
            skills=_skills_text(data.get("skills")), precision=data.get("precision", "month"),
            snippet=data.get("snippet", ""), needs_check=bool(data.get("needs_check")),
            layout_inferred=bool(data.get("layout_inferred")), rule_fallback=bool(data.get("rule_fallback")),
            existing=existing, accept=bool(data.get("default_accept")) and existing in ("new", "update"),
            original=data, description=data.get("description") or "", url=data.get("url") or "",
        )
        if posted is not None:
            row.accept = posted.get(f"entry_decision__{index}") == "accept"
            row.title = posted.get(f"entry_title__{index}", row.title)
            row.organization = posted.get(f"entry_org__{index}", row.organization)
            row.start = posted.get(f"entry_start__{index}", row.start)
            row.end = posted.get(f"entry_end__{index}", row.end)
            row.is_current = f"entry_current__{index}" in posted
            row.skills = posted.get(f"entry_skills__{index}", row.skills)
        entries.append(row)
    meta = payload.get("meta") or {}
    return Review(
        fields=rows, entries=entries, truncated=bool(meta.get("truncated")),
        skills=_skill_rows(profile, payload, posted), mode=meta.get("mode", ""),
    )


def _skill_rows(profile, payload, posted):
    """GHX6-GHX8: the proposed skills against what is stored. A skill the user
    removed is shown as such and never starts ticked."""
    stored = {row.key: row for row in ProfileSkill.objects.filter(profile=profile)}
    rows = []
    for index, data in enumerate(payload.get("skills") or []):
        row = stored.get(str(data.get("name", "")).casefold())
        if row is None:
            existing = "new"
        elif row.dismissed:
            existing = "removed"
        elif row.evidence == data.get("evidence") and row.name == data.get("name"):
            existing = "unchanged"
        else:
            existing = "update"
        accept = bool(data.get("default_accept")) and existing in ("new", "update")
        if posted is not None:
            accept = posted.get(f"skill_decision__{index}") == "accept"
        rows.append(SkillRow(
            index=index, name=data.get("name", ""), proof=data.get("proof", ""),
            existing=existing, accept=accept, original=data,
        ))
    return rows


def _entry_data(row):
    """The dict ``resume_facts`` validates for an accepted entry row, and whether
    the user changed anything from the proposal."""
    original = row.original
    start, end = row.start.strip() or None, row.end.strip() or None
    skills = _parse_skills(row.skills)
    edited = (
        _norm(row.title) != _norm(original.get("title"))
        or _norm(row.organization) != _norm(original.get("organization"))
        or start != (original.get("start") or None)
        or (None if row.is_current else end) != (None if original.get("is_current") else (original.get("end") or None))
        or row.is_current != bool(original.get("is_current"))
        or _norm(skills) != _norm(original.get("skills") or [])
    )
    dates_edited = start != (original.get("start") or None) or end != (original.get("end") or None)
    return {
        "kind": row.kind,
        "title": row.title,
        "organization": row.organization,
        "start": start,
        "end": None if row.is_current else end,
        "is_current": row.is_current,
        "precision": "month" if dates_edited else original.get("precision", "month"),
        "skills": skills,
        "description": original.get("description") or "",
        "url": original.get("url") or "",
        "edited": edited,
    }


def apply_review(job, post):
    """Write the accepted fields and entries, then close the job. All or nothing.

    Raises :class:`ReviewUnavailable` if the job is not ready or has expired, and
    :class:`ReviewError` (nothing written) if a value is invalid.
    """
    with transaction.atomic():
        job = ImportJob.objects.select_for_update().select_related("profile").get(pk=job.pk)
        if job.status != ImportJob.Status.READY or job.expires_at <= timezone.now():
            raise ReviewUnavailable()
        review = build_review(job, posted=post)

        values, edited = {}, set()
        asked_but_kept = []
        for row in review.fields:
            if row.accept and row.kept:
                asked_but_kept.append(row.label)  # R5: reported, never overwritten
            if not row.accept or row.kept or row.unchanged:
                continue
            value = _from_input(row.key, row.imported)
            if value in ("", []):
                continue
            values[row.key] = value
            if _norm(row.imported) != _norm(row.original):
                edited.add(row.key)

        entry_rows, entry_data = [], []
        for row in review.entries:
            if row.accept and row.existing not in ("kept", "unchanged"):
                entry_rows.append(row.index)
                entry_data.append(_entry_data(row))

        outcome = Outcome()
        try:
            result = profile_fields.apply_imported(job.profile_id, values, detail=str(job.public_id), edited=edited)
        except ImportApplyError as exc:
            raise ReviewError(field_errors=exc.errors) from None
        outcome.applied = [FIELD_LABELS[k] for k in result.applied]
        outcome.kept = asked_but_kept + [FIELD_LABELS[k] for k in result.kept]
        outcome.unchanged = [FIELD_LABELS[k] for k in result.unchanged]

        try:
            entries_result = apply_entries(job.profile_id, entry_data)
        except EntryValueError as exc:
            position, _, code = exc.code.partition(":")
            index = entry_rows[int(position)] if position.isdigit() and int(position) < len(entry_rows) else 0
            raise ReviewError(entry_errors={index: code or exc.code}) from None
        outcome.entries_created = len(entries_result.created)
        outcome.entries_updated = len(entries_result.updated)
        outcome.entries_kept = len(entries_result.kept)

        accepted_skills = [row for row in review.skills if row.accept and row.existing != "unchanged"]
        try:
            skills_result = profile_skills.apply_skills(
                job.profile_id,
                [{"name": r.original.get("name"), "origin": r.original.get("origin", "github"),
                  "evidence": r.original.get("evidence")} for r in accepted_skills],
                mode=review.mode or profile_skills.FULL,
            )
        except SkillValueError as exc:
            position, _, code = exc.code.partition(":")
            index = accepted_skills[int(position)].index if position.isdigit() and int(position) < len(accepted_skills) else 0
            raise ReviewError(skill_errors={index: code or exc.code}) from None
        outcome.skills_created = len(skills_result.created)
        outcome.skills_updated = len(skills_result.updated)

        job.status = ImportJob.Status.APPLIED
        job.applied_at = timezone.now()
        job.payload = {}
        job.save(update_fields=["status", "applied_at", "payload", "updated_at"])
    return outcome


def discard(job):
    """Close a ready job without applying anything; clears its proposals."""
    with transaction.atomic():
        job = ImportJob.objects.select_for_update().get(pk=job.pk)
        if job.status != ImportJob.Status.READY:
            raise ReviewUnavailable()
        job.status = ImportJob.Status.DISCARDED
        job.payload = {}
        job.save(update_fields=["status", "payload", "updated_at"])
