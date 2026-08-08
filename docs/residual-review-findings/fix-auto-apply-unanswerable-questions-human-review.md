# Residual Review Findings — fix/auto-apply-unanswerable-questions-human-review

Source: manual `ce-code-review mode:autofix` run against
`docs/plans/2026-08-09-001-fix-auto-apply-unanswerable-questions-human-review-plan.md`,
diff base `07e8e38331fe0fd71c63a98b32d18f586b1d7f43`, head after autofix commit
`03b8b9b`. 8 reviewers dispatched (correctness, testing, maintainability,
project-standards, agent-native, learnings, reliability, adversarial).
`safe_auto`-eligible findings were applied directly in commit `03b8b9b`
(whitespace-bypass fix, required-FILE-field exclusion fix, template
duplication cleanup, added regression/content tests — see that commit's
message for the itemized list).

The findings below could not be resolved in this pass because they require
either a data backfill decision or a scope decision beyond this fix — they
are documented here as durable, tracked residuals rather than silently
dropped.

## Residual Actionable Work

- **P2** (`apps/auto_apply/models.py` / `apps/web/views.py`
  `_blocking_required_fields`) — Any `AutoApplyDraft` row already sitting in
  `DRAFTED` status *before* this fix deploys (specifically, one of the old
  `LLM_CALL_FAILED`/`MISSING_LLM_RESPONSE` placeholder entries, which never
  wrote a `required` key) will not be recognized as blocking by
  `_blocking_required_fields`, since `entry.get("required")` is `None` on
  those rows. Flagged by both `correctness` (confidence 50) and
  `adversarial` (confidence 75, rated P0 in isolation since it lets a
  pre-deploy row with a genuinely blank required field become sendable with
  no gate) — synthesized down to P2 here because this entire auto-apply
  feature is unmerged (branched off `feat/auto-apply-greenhouse-email-verification`,
  not present on `master`), so no real production `AutoApplyDraft` rows
  exist yet for this gap to affect. **Action:** before or alongside merging
  this feature to `master` for the first time, either (a) confirm via a
  system check / data audit that no `DRAFTED` row lacks a `required` key on
  any answer entry, or (b) add a one-time data migration that backfills
  `required` onto existing `answers` JSON by re-deriving it from each
  draft's `form_schema_snapshot`.
- **P2** (`apps/web/views.py` `auto_apply_queue` / `send_auto_apply_draft`) —
  The new "blocked until answered" state has no JSON/API surface; a
  non-browser client can only learn which fields are blocking a draft by
  scraping the rendered `templates/web/auto_apply_queue.html`, and a
  rejected send only communicates via a Django `messages` flash + redirect,
  not a structured response. Flagged by `agent-native` (this is a
  pre-existing gap across the whole auto-apply queue UI, not introduced by
  this diff, but this PR is a natural point to close it since
  `draft.answers`/`blocking_fields` are already structured data).
  **Action:** add a JSON branch (e.g. `Accept: application/json`) or a
  dedicated endpoint exposing `{"blocking_fields": [...]}`, and consider a
  structured (e.g. 409 JSON) response from `send_auto_apply_draft` on
  rejection.

## Advisory / Out of Scope (report-only, no action required)

- **P3**, `reliability` (confidence 50) — `submit_auto_apply_draft`
  (`apps/auto_apply/tasks.py`) does not re-check required fields at actual
  submission time; it trusts the `DRAFTED -> SENDING` transition already
  implies the gate passed. A narrow, already-acknowledged TOCTOU window
  exists between `send_auto_apply_draft`'s pre-check and a concurrent
  `edit_auto_apply_draft` call re-blanking a required answer. Same-user race,
  low practical impact; not fixed here since the atomic `SENDING` transition
  remains the real safety boundary per this PR's own design (see
  `send_auto_apply_draft`'s docstring).
- **P3**, `reliability` (confidence 50) — Drafts with an unanswerable
  required custom question now persist as `DRAFTED` indefinitely instead of
  terminating as `EXCLUDED`. This is the intended behavior change (the
  entire point of this fix), bounded by the existing
  `sweep_stale_auto_apply_drafts` Celery Beat task, which eventually moves
  them to `STALE` once the job closes.
