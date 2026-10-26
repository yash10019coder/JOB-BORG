"""Submit-time confirmation gate and answer snapshot (FR7.6, FR7.9).

A draft entry flagged ``needs_confirmation`` holds a T0/T1 answer that was not
written by the user (learned or imported). It is prefilled for convenience but
may not be sent until the user explicitly confirms it in the review queue.
Both the send view and the submit task call :func:`unconfirmed_fields`, so a
draft cannot reach an employer unreviewed by either route.
"""
from apps.accounts.tiering import TIERING_VERSION
from apps.auto_apply.greenhouse_form.field_mapping import FILE


def _is_blank(value):
    if isinstance(value, (list, tuple)):
        return not any(str(item or "").strip() for item in value)
    return not str(value or "").strip()


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
        and not _is_blank(entry.get("value"))
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
