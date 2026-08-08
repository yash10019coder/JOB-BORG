---
title: "fix: float unanswerable auto-apply questions to human review instead of excluding the draft"
status: completed
created: 2026-08-09
---

# Float Unanswerable Auto-Apply Questions to Human Review

## Problem Frame

When the LLM answer-inference pipeline (`apps/auto_apply/llm/base.py`) cannot confidently answer a custom
application question, `apps/auto_apply/services/drafting.py`'s `draft_for()` currently has two failure modes,
both of which dead-end the application instead of giving a human a chance to answer:

1. **Required field, unanswerable** (hard-excluded category, insufficient evidence, ungrounded evidence, or
   low confidence with blank answer) → the *entire draft* is marked `status=EXCLUDED`,
   `reason_code=UNANSWERABLE_REQUIRED` (`apps/auto_apply/services/drafting.py:230-243`). No `answers` payload
   is persisted, so the review queue (`apps/web/views.py:auto_apply_queue`) has nothing to show and nothing
   editable — this is a terminal state today.
2. **Optional field, unanswerable** for any reason other than an LLM-infrastructure failure → the question is
   silently dropped from `answers_payload` with `continue` (`drafting.py:219-221`). It never appears in the
   review queue at all; a human reviewing the draft has no idea the question existed.

One narrow exception already exists and is the shape this plan generalizes: when the *reason* is
`LLM_CALL_FAILED` or `MISSING_LLM_RESPONSE` (the LLM pipeline itself broke, not a judgment about the
question) on a **required** field, `drafting.py:204-218` already leaves a blank, `needs_review=True`
placeholder in the draft instead of excluding it — explicitly reasoned in the code comment as "that's not a
judgment that this question is unanswerable, so don't exclude the whole draft over it." This plan extends
that same treatment to every unanswerable reason, for both required and optional fields, so "the LLM couldn't
answer" always means "a human gets to," never "this application is dropped."

This directly continues the design intent already stated in the originating plan
(`docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md:93`, citing
`docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md`): *"treat
unresolved/uncertain as an explicit first-class state rather than silent fallback."* Today that principle
holds for low-confidence/ungrounded answers (which do get `needs_review=True` entries) but not for
insufficient-evidence, hard-excluded-category, or optional-field cases — this plan closes that gap.

## Requirements

- **R1.** Every custom question that `resolve_field_answers()` processes must produce a visible entry in
  `AutoApplyDraft.answers`, regardless of field required-ness or resolution reason. No question is ever
  silently dropped from the payload.
- **R2.** A required field that the LLM could not confidently answer (any `ResolutionReason` — hard-excluded
  category, insufficient evidence, ungrounded evidence, low confidence with blank text, or LLM-infra failure)
  no longer excludes the whole draft. The draft is persisted `status=DRAFTED` with a blank,
  `needs_review=True` placeholder for that field, exactly like the existing LLM-infra-failure handling.
- **R3.** Each `answers` entry gains a `required` flag (persisted per-entry) so downstream code can tell which
  blank `needs_review` entries actually block submission versus which are optional and merely flagged for
  confirmation.
- **R4.** A draft cannot be sent to the employer while it still has a required field with a blank answer. This
  is enforced both in the UI (the Send button is disabled/hidden with an explanatory message) and, as a
  defense-in-depth backstop, in `send_auto_apply_draft`'s POST handler itself (reject with a flash message
  rather than trusting client-side gating alone).
- **R5.** The review queue UI visually distinguishes three answer states per field: confirmed
  (`needs_review=False`), needs review but optional (`needs_review=True`, `required=False`), and blocking
  (`needs_review=True`, `required=True`, blank value) — the last one needs to read as "you must answer this
  before you can send," not just "please double-check this."
- **R6.** `AutoApplyDraft.Status.EXCLUDED` / `ReasonCode.UNANSWERABLE_REQUIRED` are not deleted from the model
  (existing rows and any other future terminal-exclusion use keep working), but `drafting.py` stops producing
  them for the "LLM couldn't answer a content question" case. Document this narrowing in the model's
  docstring/comments so a future reader doesn't wonder why the reason code is now unreachable from this path.
- **R7.** Existing behavior that must *not* change: profile-derived standard fields (name/email/phone) still
  follow their current required-blank handling (`drafting.py:185-186`, unaffected by this plan — those already
  come from `Profile`, not LLM inference, and a blank one signals an incomplete profile, a different problem);
  `ExplicitAnswer` override behavior is unchanged; hard-excluded-category questions still never get an LLM
  call (only their *fallback-to-blank-placeholder* behavior changes, not the "never ask the LLM" policy);
  `FILE`-type answers remain excluded from human editing in the review queue (existing security carve-out at
  `apps/web/views.py` in `edit_auto_apply_draft`).

## Scope Boundaries

**In scope:** the drafting-time placeholder generation (R1-R3), send-time gating (R4), and review-queue
visual states (R5), for the existing `DRAFTED`/edit/send flow.

**Out of scope / Deferred to Follow-Up Work:**
- Building a UI for creating/editing `ExplicitAnswer` records (still admin-only). Hard-excluded-category
  questions will keep landing as blocking placeholders until a human either fills the specific application's
  answer inline (this plan) or an `ExplicitAnswer` exists to pre-empt the LLM call entirely (pre-existing gap,
  not introduced by this plan).
- Re-running `resolve_answers()`/the LLM after a human edits a placeholder. Once a human types an answer into
  the review queue, it's just taken as the final answer for that field on that draft (existing
  `edit_auto_apply_draft` behavior) — there's no concept of "ask the LLM again with this hint."
- Any change to `evidence_appears_in()`, the confidence threshold, or the hard-excluded-category list itself
  — this plan changes what happens *after* those checks produce an unanswerable result, not the checks
  themselves.
- FILE-type required fields the LLM/human can't fill inline (pre-existing carve-out, unchanged): a required
  file upload question with no resume-derived source stays a real gap this plan does not attempt to solve.

## Key Technical Decisions

### D1. Generalize the existing LLM-infra-failure placeholder branch to every unanswerable reason

`drafting.py:195-221`'s per-answer loop currently special-cases only `LLM_CALL_FAILED`/`MISSING_LLM_RESPONSE`
+ `required` into a placeholder; everything else either drops silently (optional) or accumulates into
`unanswerable_required` (required, → whole-draft exclusion). Collapse this to one rule: **any falsy answer,
for any reason, on any field, becomes a placeholder entry** (`value=""`, `needs_review=True`, `required=<field's
required flag>`, `category`, `reason`). The `unanswerable_required` list and the post-loop
`if unanswerable_required: return _persist_draft(..., EXCLUDED, ...)` branch (`drafting.py:230-243`) are
removed entirely — there is no longer a code path in `draft_for()`'s custom-question loop that produces
`EXCLUDED`. This is a strict generalization of already-reviewed, already-shipped logic, not new design.

### D2. Persist `required` per answer entry

Today `answers_payload` entries carry `value`, `needs_review`, `category`, `reason`, `field_type` — no
`required` flag. Add it at every write site (`drafting.py:179-184` for standard fields, the new unified
placeholder/answer write for custom fields). This is the one new piece of data the view/template need to
distinguish "must answer" from "optional, please confirm."

### D3. Send-time gating lives in the view, not the model

`AutoApplyDraft` stays schema-less on `answers` (JSONField) — no migration needed. `send_auto_apply_draft`
(`apps/web/views.py:348-372`) computes blocking state by scanning `draft.answers.values()` for any entry with
`required=True and needs_review=True and not value` immediately before flipping status to `SENDING`. If any
are found, reject the POST (redirect back to the queue with a message identifying which fields are blocking)
rather than enqueuing `submit_auto_apply_draft`. The queue view (`auto_apply_queue`) computes the same flag
per draft for template rendering (disable/hide the Send button, show a "N question(s) need an answer before
you can send this" message) — the two checks share one helper function so the gating logic isn't duplicated
or drifted between "hide the button" and "reject the click."

### D4. Review-queue template: three visual states, not two

Today the template (`templates/web/auto_apply_queue.html:38-53`) only distinguishes `needs_review` true/false
via an amber left-border + badge. Add a third state for blocking entries (`required=True` and blank) — reuse
the amber styling but change the badge text (e.g., "Answer required" vs "Needs review") and the aria-label, so
screen-reader users get the same distinction sighted users get from button-disabled state. Non-blocking
`needs_review=True` entries (optional field, or required field with *some* LLM-provided but low-confidence
text) keep today's "Needs review" treatment unchanged — R5 is additive, not a rework of the existing
low-confidence UX.

## Files

- `apps/auto_apply/services/drafting.py` — collapse the unanswerable-required branch into the unified
  placeholder path (D1); add `required` to every `answers_payload` entry (D2); remove `unanswerable_required`
  list and its post-loop `EXCLUDED` return.
- `apps/auto_apply/models.py` — docstring/comment update on `ReasonCode.UNANSWERABLE_REQUIRED` and
  `Status.EXCLUDED` noting they're no longer produced by `drafting.py`'s content-question path (R6); no schema
  change.
- `apps/web/views.py` — add a shared helper (e.g. `_blocking_required_fields(draft)`) used by both
  `auto_apply_queue` (context flag per draft) and `send_auto_apply_draft` (reject-with-message backstop).
- `templates/web/auto_apply_queue.html` — third visual state for blocking entries (D4); Send button
  disabled/hidden + explanatory message when a draft has blocking fields.
- `apps/auto_apply/tests/test_drafting_service.py` — update the two tests that currently assert `EXCLUDED` for
  unanswerable-required questions (see Test Scenarios below — these are being *rewritten*, not just extended,
  since the behavior they assert is exactly what's changing).
- `apps/web/tests/test_auto_apply_views.py` — new coverage for the send-time gating backstop and queue-context
  blocking flag.

## Implementation Units

### U1. Unify the unanswerable-question handling in `draft_for()`

**Requirements:** R1, R2, R3 | **Dependencies:** none
**Files:** `apps/auto_apply/services/drafting.py`; `apps/auto_apply/tests/test_drafting_service.py`
**Approach:** Replace the branching at `drafting.py:195-221` (falsy-answer special case for
`_LLM_INFRA_FAILURE_REASONS` + required vs. everything else) with one unconditional rule: every
`resolved_answer` with a falsy `.answer` becomes a placeholder entry (`value=""`, `needs_review=True`,
`required=form_field.required`, `category=resolved_answer.category`, `reason=resolved_answer.reason`,
`field_type=form_field.field_type`) written into `answers_payload`, no `continue` and no
`unanswerable_required.append`. Every *non-falsy* answer keeps its existing entry shape, plus the new
`required` key. Delete the `unanswerable_required` list, its two append sites (this loop and the standard-field
loop at `drafting.py:185-186` — decide during implementation whether standard fields join the same unified
list or keep their own required-blank signal per R7; R7 says standard-field behavior is unchanged, so route
standard fields to their own placeholder entry with `required=True` rather than reusing custom-question
`ResolutionReason` values, since standard fields never go through `resolve_field_answers()`) and the
post-loop `if unanswerable_required: return _persist_draft(..., EXCLUDED, ...)` block. `draft_for()` for the
custom/standard-field path now always reaches the final `_persist_draft(..., DRAFTED, answers=answers_payload,
...)` return — remove the now-unreachable second return statement.
**Patterns to follow:** The existing `LLM_CALL_FAILED`/`MISSING_LLM_RESPONSE` placeholder branch
(`drafting.py:204-218`) is the exact shape to generalize — same dict keys, same `needs_review=True`, same
"blank text, not a crash."
**Test scenarios:**
- Required custom field, `ResolutionReason.HARD_EXCLUDED_CATEGORY` → draft is `DRAFTED` (not `EXCLUDED`);
  `answers_payload[label]` has `value=""`, `needs_review=True`, `required=True`.
- Required custom field, `ResolutionReason.INSUFFICIENT_EVIDENCE` → same assertion shape as above (this
  replaces `test_required_question_with_no_explicit_and_no_confident_llm_answer_excludes`, which currently
  asserts `EXCLUDED` — rewrite it to assert `DRAFTED` + placeholder instead of deleting the coverage).
- Required custom field with no resume text at all (currently
  `test_required_llm_eligible_question_with_no_resume_text_excludes_rather_than_crashing`) → same rewrite:
  assert `DRAFTED` + placeholder, not `EXCLUDED`.
- Optional custom field, unanswerable for any reason (previously silently dropped) → `answers_payload`
  contains an entry for it with `needs_review=True`, `required=False`, `value=""` (Covers R1 — this is the new
  behavior; there is no pre-existing test to rewrite here since the old behavior was "no entry at all").
- Required custom field, `LLM_CALL_FAILED`/`MISSING_LLM_RESPONSE` — unchanged behavior, still a placeholder,
  still `DRAFTED` (regression guard: confirm the generalization didn't change this pre-existing case's output
  shape, just its code path).
- Confident/low-confidence/explicit-answer paths (already-passing tests around lines 125, 157, 195, 216) —
  regression guard that non-falsy answers are untouched by this refactor, now also asserting the new
  `required` key is present with the correct value.
- Standard required field left blank (e.g. no `Profile.email`) — confirm R7: still produces its existing
  required-blank signal (define during implementation whether this now also becomes a non-excluding
  placeholder or keeps prior behavior; if prior behavior was already "blank profile field breaks drafting
  earlier via a different path," verify that path is genuinely untouched by this unit's diff).
**Verification:** `apps/auto_apply/tests/test_drafting_service.py` green with the rewritten + new scenarios;
no other test in the file changes assertion outcome (diff review) unless intentionally noted above.

### U2. Send-time gating: shared helper + view enforcement

**Requirements:** R4 | **Dependencies:** U1
**Files:** `apps/web/views.py`; `apps/web/tests/test_auto_apply_views.py`
**Approach:** Add `_blocking_required_fields(draft) -> list[str]` (or similar) in `apps/web/views.py`,
returning the labels of any `answers` entries with `required=True`, `needs_review=True` (or simply blank
`value`, whichever is the more precise blocking signal per U1's actual entry shape), sorted for stable
message ordering. `auto_apply_queue` calls it per draft and stashes the result on the context (e.g.
`draft.blocking_fields` or a parallel dict keyed by draft id — match whatever context-building pattern the
view already uses for `_friendly_draft_message`). `send_auto_apply_draft` calls it first; if non-empty, do not
enqueue `submit_auto_apply_draft` or flip status to `SENDING` — redirect back to the queue with a message
naming the blocking fields (reuse whichever messaging mechanism `edit_auto_apply_draft`/`trigger_auto_apply`
already use for user-facing flash messages — Django's `messages` framework if already in use elsewhere in this
view module, otherwise match the existing convention).
**Patterns to follow:** `_friendly_draft_message` (`apps/web/views.py:213-222`) for the "compute a
presentation-layer derived value from `reason_code`/`answers`" shape; `edit_auto_apply_draft`'s existing
ownership/status checks for the guard-clause style.
**Test scenarios:**
- Draft with a blocking field, POST to `send_auto_apply_draft` → response does not enqueue the Celery task
  (assert via mock/`unittest.mock.patch` on the task, matching this codebase's established Celery-task
  mocking convention), draft status remains `DRAFTED`, redirect/message names the blocking field.
- Draft with only non-blocking `needs_review` entries (optional field, or low-confidence-but-present answer)
  → send proceeds exactly as today (regression guard).
- Draft with zero `needs_review` entries → send proceeds exactly as today (regression guard).
- `auto_apply_queue` context: a draft with blocking fields carries the computed flag/list in context;
  a draft without does not (or carries an empty list) — whichever shape the template consumes.
- Ownership/ownership-adjacent existing tests for `send_auto_apply_draft` (wrong user, wrong status) —
  regression guard that the new check is additive and doesn't short-circuit before existing checks.
**Verification:** `apps/web/tests/test_auto_apply_views.py` green; manually trace that a blocked send never
reaches `submit_auto_apply_draft`.

### U3. Review-queue template: blocking visual state and gated Send button

**Requirements:** R4, R5 | **Dependencies:** U2
**Files:** `templates/web/auto_apply_queue.html`
**Approach:** In the per-field loop (`auto_apply_queue.html:38-53`), branch the badge/label on the three
states from D4: confirmed (no badge, current unchanged styling), needs-review-optional (existing amber
"Needs review" badge, unchanged), blocking (amber styling reused, badge text changed to something like
"Answer required" with a distinct `aria-label`, e.g. "This question must be answered before the application
can be sent"). Around the existing Send button block (`auto_apply_queue.html:60-66`), gate on the
context flag from U2: when blocking fields exist, replace the Send form with a disabled-looking button or a
short message ("Answer N required question(s) below before sending") instead of a submittable form — do not
rely on `disabled` alone on a rendered `<button>` inside a `<form>` as the *only* protection, since U2's
server-side check is the actual enforcement; this is presentation, not security.
**Patterns to follow:** Existing inline-style conventions already used in this template (no separate CSS
file, per `auto_apply_queue.html:39-44`).
**Test scenarios:** Test expectation: none — this unit's behavior is exercised through U2's view-level tests
(context flag correctness) and manual/browser verification (visual states), matching this codebase's existing
practice of not asserting template HTML structure in Python tests beyond spot-checking key strings when
present (see whatever precedent exists in `apps/web/tests/test_auto_apply_views.py` for template-content
assertions, if any — follow that precedent's granularity rather than adding brittle full-HTML assertions).
**Verification:** Manual browser check via the project's local dev server: a draft with a blocking field shows
the new badge text and no submittable Send button; a draft with only optional `needs_review` entries still
shows the original Send button and behavior; a fully-confirmed draft is unaffected.

## Test Strategy

Django's built-in test runner (`docker exec jobborg-web-1 python manage.py test apps --settings=config.settings.test --keepdb`, the established convention in this repo per prior sessions). `TestCase`/`SimpleTestCase` per existing file conventions in `test_drafting_service.py` and `test_auto_apply_views.py`. No new test infrastructure needed — this plan changes existing, already-tested code paths and adds coverage for previously-untested silent-drop behavior (U1) and previously-nonexistent gating (U2).

Cross-cutting regression requirement: run the full `apps.auto_apply` and `apps.web` suites after each unit, not just the new/changed test file, since `draft_for()`'s output shape is a load-bearing contract for `edit_auto_apply_draft`, `send_auto_apply_draft`, and the queue template — all three consume `answers_payload`'s exact key set, and U1 adds a new key (`required`) to every entry.

## Risks

- **Removing the `EXCLUDED`/`UNANSWERABLE_REQUIRED` path changes user-visible product behavior**: users who
  previously saw "this application was excluded" will now see a draft sitting in their queue asking them to
  answer a question. This is the explicit point of the fix (per the user's framing: "it should float up so a
  human can answer"), not a regression, but it does mean drafts accumulate in the queue rather than
  terminating — worth confirming there's no separate cleanup/expiry job that assumed `EXCLUDED` was the
  natural end state for unanswerable questions (checked: no such job was found in this codebase during
  research; the sweep/timeout logic in `apps/auto_apply/tasks.py` operates on `SENDING`-status staleness, not
  `EXCLUDED`).
- **Hard-excluded-category questions (work authorization, salary, legal attestation) now always produce a
  blocking placeholder** rather than an immediate exclusion, since there is still no `ExplicitAnswer`-creation
  UI (deferred, see Scope Boundaries). This is strictly better than today (the question is now visible and
  answerable inline per-draft) but does not solve the "answer it once, reuse across drafts" ergonomics gap —
  flagged so it isn't mistaken for out-of-scope work sneaking in.
- **`required` key addition to `answers_payload`** is a shape change to data already read by
  `edit_auto_apply_draft` and the template. Confirmed via research that neither currently reads a `required`
  key, so this is purely additive — but verify during implementation that no code does a strict key-set
  comparison (e.g. `assertEqual(entry, {...exact dict...})`) that would break on the new key; several existing
  tests in `test_drafting_service.py` do assert on individual keys (`entry["needs_review"]`), which is
  additive-safe, but a full-dict equality assertion would need updating.

## Verification

- Full `apps.auto_apply` + `apps.web` test suites green.
- Manual queue walkthrough: trigger auto-apply against a job with a custom question the LLM can't ground
  (e.g., a question with no matching resume text) — confirm the draft lands `DRAFTED` with a visible,
  clearly-labeled blocking placeholder, Send is unavailable until answered, and after typing an answer via the
  existing edit flow the blocking state clears and Send becomes available.
