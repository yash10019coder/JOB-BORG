"""The "questions I've been asked" panel on the Answers page.

Built from the user's last 200 drafts: every distinct question (normalized, so
the same question across employers is one row) with how often it appeared,
when it was last seen, and whether the profile can already answer it. Cached
per user for an hour; anything that could change it calls
``invalidate_questions_panel`` (a new draft, a saved or deleted answer, a
settings save).

Questions answered from typed settings (work authorization, citizenship,
salary, contact/location) link to the setting instead of offering quick-fill:
a legal or commercial value is never pushed into a typed field from here.
"""
from __future__ import annotations

from django.core.cache import cache

from apps.accounts import question_semantics
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import (
    load_bank_rows,
    normalize_question_key,
    resolve_answer,
)
from apps.accounts.services.panel_cache import PANEL_CACHE_TTL, panel_cache_key
from apps.accounts.tiering import QuestionCategory, classify
from apps.auto_apply.greenhouse_form.field_mapping import FILE, TEXTAREA
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services import drafting

PANEL_DRAFT_LIMIT = 200

# (id, title, anchor on the Answers page for settings-backed groups)
GROUPS = (
    ("work_auth", "Work authorization", "work-auth"),
    ("citizenship", "Citizenship", "citizenship"),
    ("salary", "Salary", "salary"),
    ("contact", "Contact & location", "contact"),
    ("eeo", "Diversity & demographic", ""),
    ("legal", "Legal & background", ""),
    ("other", "Other questions", ""),
)
_KIND_GROUP = {
    typed_facts.AUTHORIZATION: "work_auth",
    typed_facts.SPONSORSHIP: "work_auth",
    typed_facts.CITIZENSHIP: "citizenship",
    typed_facts.SALARY: "salary",
    typed_facts.COUNTRY: "contact",
    typed_facts.CITY: "contact",
    typed_facts.LOCATION: "contact",
    typed_facts.ADDRESS: "contact",
    typed_facts.TIMEZONE: "contact",
    typed_facts.MEMBERSHIP: "contact",
}
_CATEGORY_GROUP = {
    QuestionCategory.WORK_AUTHORIZATION: "work_auth",
    QuestionCategory.DEMOGRAPHIC: "eeo",
    QuestionCategory.LEGAL_ATTESTATION: "legal",
    QuestionCategory.BACKGROUND_CHECK: "legal",
    QuestionCategory.SALARY_EXPECTATION: "salary",
}
_REASON_TEXT = {
    "no_country_entry": "no entry for that country",
    "no_country": "country not clear from the question",
    "country_unresolved": "place not recognised",
    "multiple_countries": "more than one country named",
    "status_unknown": "your status doesn't settle it",
    "restricted_authorization": "asks about authorization without restriction",
    "no_option_match": "no matching option on the form",
    "no_citizenship_entered": "no citizenship entered",
    "multiple_citizenships": "you hold more than one citizenship",
    "no_region": "no salary region for that job",
    "no_band": "no band set for that region",
    "not_entered": "not entered yet",
    "phone_dial_mismatch": "country code doesn't match your phone number",
    "place_unresolved": "place can't be checked against your location",
    "": "this question needs a person",
}


def get_panel(user):
    """The cached panel for ``user``, building it on a miss."""
    key = panel_cache_key(user.pk)
    panel = cache.get(key)
    if panel is None:
        panel = build_panel(user)
        cache.set(key, panel, PANEL_CACHE_TTL)
    return panel


def _snapshot_fields(draft):
    return (draft.form_schema_snapshot or {}).get("fields") or []


def _aggregate(user):
    """``{normalized key: info}`` over the last drafts, newest first."""
    drafts = (
        AutoApplyDraft.objects.filter(user=user)
        .exclude(form_schema_snapshot__isnull=True)
        .select_related("job", "job__employer")
        .order_by("-created_at")[:PANEL_DRAFT_LIMIT]
    )
    seen = {}
    for draft in drafts:
        employer = getattr(getattr(draft.job, "employer", None), "name", None)
        for index, field in enumerate(_snapshot_fields(draft)):
            if field.get("field_type") == FILE:
                continue
            label = field.get("label") or ""
            key = normalize_question_key(
                label, None if field.get("field_type") == TEXTAREA else employer
            )
            info = seen.get(key)
            if info is None:
                info = seen[key] = {
                    "label": label,
                    "field_type": field.get("field_type") or "",
                    "options": tuple(field.get("options") or ()),
                    "count": 0,
                    "last_seen": draft.created_at,
                    "draft_id": draft.pk,
                    "field_index": index,
                    "job": draft.job,
                }
            info["count"] += 1
    return seen


def _row(info, profile, user, bank_rows):
    label, options, job = info["label"], info["options"], info["job"]
    base = {
        "label": label,
        "short": label if len(label) <= 90 else label[:87] + "…",
        "count": info["count"],
        "last_seen": info["last_seen"],
        "answered": False,
        "status": "",
        "source": "",
        "anchor": "",
        "quick_fill": None,
    }
    standard = drafting._classify_standard_field(label) if info["field_type"] in ("text", "file") else None
    if standard:
        have = bool(drafting._standard_field_value(standard, profile, user))
        base.update(
            group="contact", answered=have, anchor="contact",
            status="answered by your settings" if have else "not covered (not entered yet)",
            source="settings" if have else "",
        )
        return base

    fact = typed_facts.resolve(profile, label, options=options, job=job)
    category = classify(label)
    group = (
        _KIND_GROUP.get(fact.kind) if fact is not None
        else _CATEGORY_GROUP.get(category, "other")
    )
    # An ambiguous or unmatched question about status/salary still belongs
    # with its setting, not in quick-fill.
    if fact is None and group in ("work_auth", "salary") and category != QuestionCategory.GENERIC:
        base.update(
            group=group, anchor=dict((g[0], g[2]) for g in GROUPS)[group],
            status="not covered (this wording needs a person)",
        )
        return base
    if fact is not None:
        anchor = dict((g[0], g[2]) for g in GROUPS)[group]
        if fact.value is not None:
            base.update(group=group, answered=True, anchor=anchor,
                        status="answered by your settings", source="settings")
        else:
            base.update(group=group, anchor=anchor,
                        status="not covered (" + _REASON_TEXT.get(fact.reason, fact.reason) + ")")
        return base

    found = resolve_answer(profile, label, job, options=options, bank_rows=bank_rows)
    base["group"] = group
    if found is not None and found.value is not None:
        base.update(answered=True, status="answered", source=found.provenance.get("source", ""))
    else:
        base["status"] = "not answered yet"
        base["quick_fill"] = (
            None if info["field_type"] == TEXTAREA
            else {"draft": info["draft_id"], "field": info["field_index"]}
        )
        if info["field_type"] == TEXTAREA:
            base["status"] = "not answered yet (long answers are specific to one employer)"
    return base


def build_panel(user):
    profile = user.profile
    bank_rows = load_bank_rows(profile)
    seen = _aggregate(user)
    rows = [_row(info, profile, user, bank_rows) for info in seen.values()]
    rows.sort(key=lambda r: (-r["count"], r["label"].lower()))
    groups = []
    for group_id, title, anchor in GROUPS:
        members = [r for r in rows if r["group"] == group_id]
        groups.append(
            {
                "id": group_id,
                "title": title,
                "anchor": anchor,
                "rows": members,
                "total": len(members),
                "answered": sum(1 for r in members if r["answered"]),
            }
        )
    return {"groups": groups, "question_count": len(rows)}
