# Residual Review Findings — fix/auto-apply-field-type-constrained-answers

Source: `ce-code-review mode:autofix` run against
`docs/plans/2026-08-09-002-fix-auto-apply-field-type-constrained-answers-plan.md`,
diff base `2463f5473b1e1945be3bf3f53f8d752720622434`, head `19588a1`.
7 reviewers were dispatched (correctness, testing, maintainability,
project-standards, agent-native, learnings, adversarial). `learnings`,
`project-standards`, and `agent-native` completed; `correctness`, `testing`,
`maintainability`, and `adversarial` failed mid-run due to a session API
spend-limit error before returning structured findings and could not be
retried in this pass (see Coverage below). `project-standards` and
`learnings` reported no findings. `agent-native` completed and is
summarized below.

## Residual Actionable Work

- **P2** (`apps/web/views.py` `edit_auto_apply_draft` /
  `templates/web/auto_apply_queue.html`) — The new option-bearing `<select>`
  in the review queue has no server-side enforcement: `edit_auto_apply_draft`
  still writes any POSTed `value__<i>` unconditionally (except for
  `FILE`-type fields) with no check against the answer entry's `options`.
  A non-browser client, or a modified browser request, can submit a value
  outside the real option set and have it saved as `needs_review=False`
  (i.e. it looks confirmed) — the only backstop is `GreenhouseFormClient`'s
  `select_option` failing at actual send time (U5's typed
  `GreenhouseFormSubmissionFailed`), which surfaces as a late submission
  failure rather than an immediate, correctable validation error. Flagged
  by `agent-native`. **This is a known, already-documented scope decision**
  — the plan's own "Deferred to Follow-Up Work" section explicitly named
  this exact gap and deferred it: "a determined API caller bypassing the UI
  is a pre-existing gap for this whole review-queue feature, not unique to
  option-bearing fields." Recorded here as a durable pointer rather than
  silently dropped, not as a new finding requiring an unplanned fix.
  **Action:** add server-side option validation to `edit_auto_apply_draft`
  when picking up the pre-existing gap this plan explicitly deferred.
- **P2** (`apps/web/views.py` `auto_apply_queue` /
  `templates/web/auto_apply_queue.html`) — `entry.options` (a real,
  well-defined enumeration, unlike the freeform `blocking_fields` gap noted
  in the prior fix's residual findings) is only ever serialized into
  rendered `<option>` tags; a non-browser client has no JSON way to
  discover which values are valid for a given field. Flagged by
  `agent-native` as concretizing (not newly creating) the same
  pre-existing "HTML-only queue" gap already tracked in
  `docs/residual-review-findings/fix-auto-apply-unanswerable-questions-human-review.md`.
  **Action:** when a JSON/API surface is eventually added for the queue
  (per that earlier residual finding), include each answer entry's
  `options` list.

## Advisory / Out of Scope (report-only, no action required)

- `project-standards`: no violations found — the repo's only standards
  file (root `AGENTS.md`) has no rules applicable to this Python/HTML diff.
- `learnings`: no relevant prior `docs/solutions/` entries exist for the
  `auto_apply` answer-resolution/drafting/Greenhouse-form-client area.

## Coverage

- **Reviewers that did not complete:** `correctness`, `testing`,
  `maintainability`, `adversarial` all failed mid-run with
  `"Agent terminated early due to an API error: You've hit your monthly
  spend limit"` — a session/account-level quota, not a defect in the diff
  or the review pipeline. Partial transcripts from `adversarial` and
  `correctness` before failure surfaced no new findings beyond what
  `agent-native` already reported (adversarial's partial trace confirmed
  Django's template auto-escaping prevents XSS via `{{ option }}`, and
  independently converged on the same `edit_auto_apply_draft`
  no-server-side-validation gap `agent-native` reported in full).
  **These four reviewers should be re-run** (standalone
  `ce-code-review base:2463f5473b1e1945be3bf3f53f8d752720622434` on this
  branch) once the account's spend limit resets, before this branch is
  considered fully reviewed.
- The full project test suite (`apps.auto_apply` + `apps.web`, 283 tests)
  passed after implementation, independent of this review pass.
