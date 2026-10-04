"""Submit-time confirmation gate and answer snapshot (FR7.6, FR7.9).

A draft entry flagged ``needs_confirmation`` holds an answer that was not
written by the user: a legal/commercial (T0/T1) one that was imported, or any
learned one. It is prefilled for convenience but
may not be sent until the user explicitly confirms it in the review queue.
Both the send view and the submit task call :func:`unconfirmed_fields`, so a
draft cannot reach an employer unreviewed by either route.
"""
from apps.accounts.tiering import TIERING_VERSION
from apps.auto_apply.greenhouse_form.field_mapping import FILE


def is_blank_answer_value(value) -> bool:
    """A list/tuple value (multi-select, checkbox group) is blank when every
    item is blank -- ``str(["  "])`` is a non-empty string and would otherwise
    read as "answered". The one definition of "blank" shared by drafting, the
    review views and the send gate."""
    if isinstance(value, (list, tuple)):
        return not any(str(item or "").strip() for item in value)
    return not str(value or "").strip()


def confirm_entry(entry, origin="draft_review"):
    """Mark one held draft entry as explicitly confirmed by the user.

    The one place that performs the confirmation transition, so confirming a
    field by hand and "confirm all learned answers" leave identical entries
    (apart from ``origin``, which tells them apart in the audit snapshot and
    the shadow metrics). The previous provenance is kept in ``confirmed_from``.
    """
    entry["confirmed_from"] = entry.get("provenance")
    entry["provenance"] = {
        "origin": origin,
        "source": "user",
        "locked": False,
        "confidence": 1.0,
        "detail": {},
    }
    entry["needs_confirmation"] = False
    entry["needs_review"] = False
    entry["user_confirmed"] = True


def unconfirmed_fields(answers):
    """Labels of entries the user still has to confirm before sending.

    A blank optional placeholder has nothing to send and so nothing to
    confirm; only an entry holding a real value blocks.
    """
    return sorted(
        label
        for label, entry in (answers or {}).items()
        if isinstance(entry, dict)
        and entry.get("needs_confirmation")
        and not is_blank_answer_value(entry.get("value"))
    )


def build_submit_snapshot(answers):
    """The exact answers about to be submitted, with where each came from.

    Written once, at the moment the draft moves to SENDING, so a disputed
    legal answer can be traced to its source after the fact.
    """
    snapshot = {}
    for label, entry in (answers or {}).items():
        if not isinstance(entry, dict):
            continue
        snapshot[label] = {
            "value": "[file]" if entry.get("field_type") == FILE else entry.get("value"),
            "category": entry.get("category"),
            "reason": entry.get("reason"),
            "tier": entry.get("tier"),
            "user_confirmed": bool(entry.get("user_confirmed")),
            "provenance": entry.get("provenance"),
            "confirmed_from": entry.get("confirmed_from"),
        }
    return {"tiering_version": TIERING_VERSION, "answers": snapshot}
