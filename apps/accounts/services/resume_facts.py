"""Stored resume entries (experience / projects) and what is computed from them.

``ResumeEntry`` rows come from the import review page. This module is the only
writer: it validates every entry (rules V3, EX4, EX8, EX12), upserts by natural
key without ever deleting (R7), and computes **years per skill** in code (Y1-Y3)
from the entry dates, never from a number written in the resume (EX5).

The computed years are shown to the user; they are deliberately not used to
answer application questions yet (Y4).
"""
import math
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.accounts.importing.validators import ImportValueError, clean_text
from apps.accounts.models import Profile, ResumeEntry

MIN_YEAR = 1970
MAX_SPAN_YEARS = 50
MAX_SKILLS = 30
MAX_SKILL_LEN = 40
MAX_TITLE = 80
MAX_ORG = 100


class EntryValueError(ValueError):
    """An entry failed validation. ``code`` is short and safe to show or log."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_month(value):
    """``'2022-03'`` / ``date`` / ``None`` -> ``date(2022, 3, 1)`` or ``None``."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.replace(day=1)
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(value).strip())
    if not match:
        raise EntryValueError("bad_date")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise EntryValueError("bad_date")
    return date(year, month, 1)


def dates_plausible(start, end, is_current, today=None):
    """EX4: 1970 <= year <= now, start <= end, no future end unless current,
    span <= 50 years. ``start``/``end`` are month ``date``s or ``None``."""
    today = (today or timezone.localdate()).replace(day=1)
    for value in (start, end):
        if value is not None and not (MIN_YEAR <= value.year <= today.year):
            return False
    if start is not None and start > today:
        return False
    if is_current:
        return end is None
    if end is not None and end > today:
        return False
    if start is not None and end is not None:
        if end < start:
            return False
        if (end.year - start.year) > MAX_SPAN_YEARS:
            return False
    return True


def natural_key(kind, organization, title, start):
    """Identifies "the same entry" across re-imports (R7)."""
    month = start.strftime("%Y-%m") if start else "nodate"
    parts = [(organization or "").casefold().strip(), (title or "").casefold().strip(), month]
    return "|".join(re.sub(r"\s+", " ", part) for part in parts)[:255]


def clean_skills(raw):
    """Skills: short, deduplicated case-insensitively (first spelling wins), at
    most 30. Skill names keep their characters (``C++``, ``.NET``, ``Node.js``)."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise EntryValueError("bad_skills")
    out, seen = [], set()
    for item in raw:
        try:
            skill = clean_text(str(item), max_len=MAX_SKILL_LEN, min_len=1)
        except ImportValueError:
            raise EntryValueError("bad_skill") from None
        if skill.casefold() not in seen:
            seen.add(skill.casefold())
            out.append(skill)
    if len(out) > MAX_SKILLS:
        raise EntryValueError("too_many_skills")
    return out


def clean_entry(data, today=None):
    """Validate one entry dict and return the cleaned values.

    Input keys: ``kind``, ``title``, ``organization``, ``start`` / ``end``
    (``YYYY-MM`` or ``date``), ``is_current``, ``precision``, ``skills``.
    """
    kind = data.get("kind")
    if kind not in ResumeEntry.Kind.values:
        raise EntryValueError("bad_kind")
    try:
        title = clean_text(data.get("title") or "", max_len=MAX_TITLE, min_len=2)
        organization = data.get("organization") or ""
        organization = clean_text(organization, max_len=MAX_ORG, min_len=2) if organization else ""
    except ImportValueError as exc:
        raise EntryValueError(f"bad_text_{exc.code}") from None
    if title.endswith("."):
        raise EntryValueError("sentence_title")
    start, end = parse_month(data.get("start")), parse_month(data.get("end"))
    is_current = bool(data.get("is_current"))
    if is_current:
        end = None
    if not dates_plausible(start, end, is_current, today):
        raise EntryValueError("bad_dates")
    precision = data.get("precision") or ResumeEntry.Precision.MONTH
    if precision not in ResumeEntry.Precision.values:
        raise EntryValueError("bad_precision")
    return {
        "kind": kind,
        "title": title,
        "organization": organization,
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "precision": precision,
        "skills": clean_skills(data.get("skills")),
    }


@dataclass
class EntriesResult:
    created: list = dc_field(default_factory=list)
    updated: list = dc_field(default_factory=list)
    unchanged: list = dc_field(default_factory=list)
    kept: list = dc_field(default_factory=list)  # existing user-edited rows


_COMPARED = ("title", "organization", "start_date", "end_date", "is_current", "precision", "skills")


def apply_entries(profile_id, entries, *, today=None):
    """Upsert accepted entries (R7). Never deletes.

    Each dict is :func:`clean_entry` input plus an optional ``edited`` flag (the
    user changed it on the review page, so it is stored with ``source=user``).
    An existing ``imported`` row is replaced; an existing ``user`` row is kept.
    Any invalid entry raises :class:`EntryValueError` before anything is written.
    All-or-nothing.
    """
    cleaned = []
    for index, data in enumerate(entries):
        try:
            values = clean_entry(data, today)
        except EntryValueError as exc:
            raise EntryValueError(f"{index}:{exc.code}") from None
        cleaned.append((values, bool(data.get("edited"))))

    result = EntriesResult()
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(pk=profile_id)
        for values, edited in cleaned:
            key = natural_key(values["kind"], values["organization"], values["title"], values["start_date"])
            existing = ResumeEntry.objects.filter(
                profile=profile, kind=values["kind"], natural_key=key
            ).first()
            if existing is None:
                ResumeEntry.objects.create(
                    profile=profile,
                    natural_key=key,
                    source=ResumeEntry.Source.USER if edited else ResumeEntry.Source.IMPORTED,
                    **values,
                )
                result.created.append(key)
            elif existing.source == ResumeEntry.Source.USER:
                result.kept.append(key)
            elif all(getattr(existing, name) == values[name] for name in _COMPARED) and not edited:
                result.unchanged.append(key)
            else:
                for name in _COMPARED:
                    setattr(existing, name, values[name])
                if edited:
                    existing.source = ResumeEntry.Source.USER
                existing.save()
                result.updated.append(key)
    return result


def delete_entry(profile, pk):
    """Delete one of the profile's own entries. ``True`` if it existed (R8)."""
    deleted, _ = ResumeEntry.objects.filter(profile=profile, pk=pk).delete()
    return bool(deleted)


# --------------------------------------------------------------------------
# Years per skill (Y1-Y3): computed on read, never stored.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SkillYears:
    name: str
    months: int
    approximate: bool


def _month_index(value):
    return value.year * 12 + (value.month - 1)


def years_by_skill(profile, today=None):
    """Months of experience per skill, most first.

    Only experience entries with a start date count (projects have no reliable
    duration; an entry with a single date is treated as undated). A role with no
    end date that is marked current runs to ``today``. Roles that overlap are
    merged so concurrent jobs are not counted twice (Y1); gaps are not counted.
    Skill names match case-insensitively; the spelling shown is the one used in
    the most recent role (entries come newest first).
    """
    today = (today or timezone.localdate()).replace(day=1)
    intervals, names, approximate = {}, {}, set()
    entries = ResumeEntry.objects.filter(
        profile=profile, kind=ResumeEntry.Kind.EXPERIENCE, start_date__isnull=False
    )
    for entry in entries:
        end = today if entry.is_current else entry.end_date
        if end is None:
            continue  # a single date: undated for years (EX2)
        span = (_month_index(entry.start_date), _month_index(end))
        if span[1] < span[0]:
            continue
        for skill in entry.skills or []:
            key = skill.casefold()
            names.setdefault(key, skill)
            intervals.setdefault(key, []).append(span)
            if entry.precision == ResumeEntry.Precision.YEAR:
                approximate.add(key)

    result = []
    for key, spans in intervals.items():
        spans.sort()
        total, (cur_start, cur_end) = 0, spans[0]
        for start, end in spans[1:]:
            if start <= cur_end:  # shares a month: overlapping roles
                cur_end = max(cur_end, end)
            else:
                total += cur_end - cur_start + 1
                cur_start, cur_end = start, end
        total += cur_end - cur_start + 1
        result.append(SkillYears(names[key], total, key in approximate))
    result.sort(key=lambda item: (-item.months, item.name.casefold()))
    return result


def format_years(months):
    """Y3: "< 1 yr" under 3 months, otherwise rounded to the nearest half year."""
    if months < 3:
        return "< 1 yr"
    years = math.floor(months / 6 + 0.5) / 2
    return "1 yr" if years == 1 else f"{years:g} yrs"
