# Residual Review Findings — fix/auto-apply-field-type-constrained-answers

Source: `ce-code-review mode:autofix` run against
`docs/plans/2026-08-09-002-fix-auto-apply-field-type-constrained-answers-plan.md`,
diff base `2463f5473b1e1945be3bf3f53f8d752720622434`. 7 reviewers were
dispatched (correctness, testing, maintainability, project-standards,
agent-native, learnings, adversarial) across two passes — the first pass
(head `19588a1`) hit a session spend-limit error on 4 of 7 reviewers
mid-run; those four (correctness, testing, maintainability, adversarial)
were re-dispatched against the same diff once the limit reset and all
completed. `project-standards` and `learnings` reported no findings.
`agent-native` completed on the first pass with 2 findings (below).

## Applied Fixes

Two real, `safe_auto`-eligible bugs surfaced by the re-run and fixed in
`47faac0`:

- **`ExplicitAnswer` bypassed the new option-constraint validation
  entirely** (`correctness`, confidence 75) — `resolve_field_answers()`
  built the `ResolvedAnswer` for a saved `ExplicitAnswer` directly, never
  routing it through `_enforce_option_constraint()` the way the LLM branch
  does. A saved explicit answer for e.g. `WORK_AUTHORIZATION` could
  silently violate an option-bearing field's real option set — exactly the
  class of bug this PR otherwise closes. Fixed by wrapping the explicit-
  answer branch in the same validation call. Regression tests added in
  `ExplicitAnswerOptionConstraintTests`.
- **`"|"`-joined prompt options broke for option labels containing
  `"|"`** (`adversarial`, confidence 70) — `_build_prompt()` rendered a
  question's option list as a single `options="A|B|C"` attribute; a real
  employer option label containing a literal `|` (e.g.
  `"Full-time | Part-time"`) collapsed into an ambiguous string the model
  couldn't reliably reconstruct, causing the deterministic validation gate
  to reject every answer for that question on every call — a systematic,
  self-inflicted failure, not a rare model error. Fixed by switching to one
  `<option>` element per value (no delimiter, no collision class).
  Regression test added:
  `test_option_containing_delimiter_like_punctuation_survives_intact`.

Also added on the same pass (testing-reviewer gaps, no bug fix required):
coverage for `MULTI_SELECT`/`CHECKBOX_GROUP` through the validation gate,
and `<select>` pre-selection of an already-valid stored value.

Full suite (`apps.auto_apply` + `apps.web`) green at 290 tests after these
fixes (up from 283 before this review pass).

## Residual Actionable Work

- **P2** (`apps/web/views.py` `edit_auto_apply_draft` /
  `templates/web/auto_apply_queue.html`) — The option-bearing `<select>` in
  the review queue has no server-side enforcement: `edit_auto_apply_draft`
  still writes any POSTed `value__<i>` unconditionally (except for
  `FILE`-type fields) with no check against the answer entry's `options`.
  A non-browser client, or a modified browser request, can submit a value
  outside the real option set and have it saved as `needs_review=False`
  (looks confirmed) — the only backstop is `GreenhouseFormClient`'s
  `select_option` failing at actual send time, surfacing as a late,
  unlabeled submission failure rather than an immediate, correctable
  validation error. Flagged independently by `agent-native` and
  `adversarial`. **This is a known, already-documented scope decision** —
  the plan's own "Deferred to Follow-Up Work" section explicitly named
  this exact gap: "a determined API caller bypassing the UI is a
  pre-existing gap for this whole review-queue feature, not unique to
  option-bearing fields." Recorded here as a durable pointer, not a new
  finding requiring an unplanned fix. **Action:** add server-side option
  validation to `edit_auto_apply_draft` when picking up the pre-existing
  gap this plan explicitly deferred.
- **P2** (`apps/web/views.py` `auto_apply_queue` /
  `templates/web/auto_apply_queue.html`) — `entry.options` (a real,
  well-defined enumeration, unlike the freeform `blocking_fields` gap noted
  in the prior fix's residual findings) is only ever serialized into
  rendered `<option>` tags; a non-browser client has no JSON way to
  discover which values are valid for a given field. Flagged by
  `agent-native` as concretizing (not newly creating) the same
  pre-existing "HTML-only queue" gap already tracked in
  `docs/residual-review-findings/fix-auto-apply-unanswerable-questions-human-review.md`.
  **Action:** when a JSON/API surface is eventually added for the queue,
  include each answer entry's `options` list.
- **P2** (`apps/auto_apply/services/answer_resolution.py`
  `_enforce_option_constraint`) — `QuestionAnswer.answer` is a single
  string (a pre-existing shape, not introduced by this diff); a genuinely
  multi-value `MULTI_SELECT`/`CHECKBOX_GROUP` answer (e.g. "Python and Go")
  can never satisfy an exact-match check against individual option labels,
  so those questions will always route to `needs_review` rather than ever
  auto-confirming, even when the LLM correctly identifies multiple valid
  selections. Flagged by `correctness` and `adversarial`. Documented and
  test-covered as current behavior (`MultiValueOptionFieldTests`) rather
  than silently accepted — the conservative fail-to-review outcome is
  strictly safer than the pre-existing behavior (a free-text guess that
  could fail at submit time), so this is not a regression, but a genuine
  multi-value answer path is not yet implemented. **Action:** if
  `MULTI_SELECT`/`CHECKBOX_GROUP` questions turn out to need real LLM
  multi-select answers in practice, extend `QuestionAnswer` to carry a
  list-of-strings answer for these field types and validate each element
  against `options`, rather than reusing the single-string shape.

## Advisory / Out of Scope (report-only, no action required)

- `project-standards`: no violations found — the repo's only standards
  file (root `AGENTS.md`) has no rules applicable to this Python/HTML diff.
- `learnings`: no relevant prior `docs/solutions/` entries exist for the
  `auto_apply` answer-resolution/drafting/Greenhouse-form-client area.
- `maintainability` (P3, confidence 40): `SINGLE_SELECT`'s new
  `except Exception` in `client.py` omits the `# noqa: BLE001` marker its
  sibling `_fill_combobox` carries — cosmetic only, not fixed.
- `correctness` (confidence 40): the broad `except Exception` around
  `SINGLE_SELECT`'s `select_option()` call could in principle mask an
  unrelated exception type, but this exactly mirrors the pre-existing,
  already-accepted `_fill_combobox`/`_fill_checkbox_group` pattern it was
  built to match — not a new risk introduced by this diff.
- `correctness` (confidence 30): the review-queue `<select>` silently
  falls back to the unselected `-- Select --` placeholder when
  `entry.value` doesn't match any real option (e.g. after a live-form
  option set changes underneath a draft) rather than surfacing that it
  dropped a stale value — minor UX gap, not a functional bug.
- `adversarial` (P3, confidence 65): an invalid value that somehow reaches
  `edit_auto_apply_draft` unvalidated (see the first Residual Actionable
  Work item) wastes a full browser run before failing with a generic,
  unlabeled `GreenhouseFormSubmissionFailed` — same root cause as that
  deferred item, not a separate action.
- `adversarial`: draft-time `options` snapshot staleness vs. the live form
  is already handled by the existing `schema_matches()` drift detection,
  unaffected by this diff. Django's autoescaping neutralizes any
  HTML/script content in a rendered `<option>` value — no XSS path found.
