---
title: Fix standalone-checkbox invisibility and conditionally-revealed required fields in Greenhouse auto-apply
type: fix
status: completed
date: 2026-08-09
---

# Fix Standalone-Checkbox Invisibility and Conditionally-Revealed Required Fields in Greenhouse Auto-Apply

## Summary

Two confirmed gaps in `apps/auto_apply/greenhouse_form/client.py`'s field discovery/fill pipeline, found during code investigation (not yet observed as a live failure, unlike the two prior fixes on this branch):

1. **Standalone policy/terms checkboxes are invisible.** `_checkbox_group_field()` only recognizes checkboxes wrapped in a `<fieldset><legend>` (Greenhouse's multi-select-question markup). A single standalone `<input type="checkbox">` with its own `<label>` (e.g. "I agree to the Terms of Service") returns `None` from that function, and the caller's `continue` (client.py:495-509) means the checkbox never enters `FormSchema.fields` at all. It is not flagged `GreenhouseFormSchemaMismatch` like every other unsupported-but-required field — it is simply never seen. `_blocking_required_fields()` (client.py:1063-1101) can't catch this either, since it only iterates fields already in the schema. A required standalone consent checkbox could silently leave the form unsubmittable with no signal anywhere in the pipeline.

2. **Conditionally-revealed fields are never discovered.** `submit()` calls `_discover_schema()` exactly once (client.py:340), then `_fill_answers()` fills every field from that single static snapshot with no re-scan between individual field fills (client.py:350, 851-904). A field that only appears in the DOM as a side effect of an earlier answer (the classic "select Other → reveal a specify text box" pattern) is never discovered and never filled.

Both gaps share the same underlying failure mode: silent, undetected incompleteness rather than the codebase's established fail-closed posture (`GreenhouseFormSchemaMismatch` for a known-unsupported required field, `schema_matches()` drift detection for a changed form between draft and send).

---

## Requirements

- R1. A standalone checkbox (no enclosing `<fieldset>`) with its own `<label>` is discovered as a first-class schema field, not silently skipped.
- R2. A required standalone checkbox with no fill strategy fails closed the same way every other unsupported required field type does (`GreenhouseFormSchemaMismatch`), not silently.
- R3. A standalone checkbox is fillable: `_fill_answers()` can check/uncheck it based on a resolved Yes/No answer.
- R4. Consent/attestation-style standalone checkboxes are never auto-checked by LLM inference alone — they route through the same `needs_review` / hard-exclusion path every legally-sensitive question already goes through (`QuestionCategory.LEGAL_ATTESTATION` → `HARD_EXCLUDED_CATEGORIES` in `apps/auto_apply/llm/categories.py`).
- R5. After the initial fill pass, `submit()` performs exactly one bounded re-discovery pass (not a polling loop) to detect any *new* required field that appeared as a side effect of the fills just performed.
- R6. If a new, unanswerable required field is detected by that re-discovery pass, `submit()` fails closed with a clear, typed error (reusing `GreenhouseFormSchemaMismatch`) naming the field, rather than attempting to submit an incomplete form or hanging on the existing generic timeout path.
- R7. Existing forms with no standalone checkboxes and no conditionally-revealed fields are entirely unaffected (no behavior change, no extra Playwright round-trip cost beyond the one bounded re-discovery pass).

## Scope Boundaries

- Not in scope: expanding `QuestionCategory` keyword patterns in `apps/auto_apply/llm/categories.py` beyond what already exists. `"i agree to the terms"` is already a `LEGAL_ATTESTATION` pattern; broadening coverage for other real-world consent phrasing ("By checking this box you consent to...", "I have read and agree to the Privacy Policy") is a separate, focused change with its own research need (real captured examples), not bundled here.
- Not in scope: actually *answering* a newly-revealed conditional field at submit time. There is no LLM-resolved answer for a field that didn't exist at draft time — the correct, honest behavior is fail-closed (R6), not a best-effort guess. Making conditional fields answerable end-to-end (e.g., re-running discovery+LLM-resolution mid-draft, before submit) is a materially larger feature and is deferred.
- Not in scope: multiple re-discovery passes or an unbounded reveal-and-refill loop. R5 is deliberately a single, bounded pass — see Key Technical Decisions.
- Not in scope: any change to the existing `CHECKBOX_GROUP` (`<fieldset><legend>`) handling, which is unaffected by this plan.

### Deferred to Follow-Up Work

- Broadening `LEGAL_ATTESTATION`/other category keyword coverage once real standalone-checkbox copy has been captured live.
- End-to-end support for actually answering a conditionally-revealed field (would require draft-time, not just submit-time, handling).

---

## Key Technical Decisions

### D1. Standalone checkbox becomes its own supported field type, not folded into `CHECKBOX_GROUP`

A new field type, `CHECKBOX_ACKNOWLEDGEMENT`, is added to `field_mapping.py` alongside the existing seven. Reusing `CHECKBOX_GROUP` for a single ungrouped checkbox would be a type lie: `CHECKBOX_GROUP`'s whole contract (`_fill_checkbox_group()`, `_checkbox_group_field()`) is built around a `<fieldset>` and an `options` list of sibling choices ("select all that apply"); a standalone checkbox has exactly one binary state (checked/unchecked) and no sibling options. Keeping them distinct keeps each fill function's contract simple and matches the existing pattern of one field type per distinct rendered-control shape (`SINGLE_SELECT` vs `MULTI_SELECT` vs `COMBOBOX_SELECT` are already split the same way for the same reason).

### D2. Model the checkbox as an option-bearing field with a fixed `("Yes", "No")` option set

Rather than inventing a new boolean-typed branch through `Question`/`ResolvedAnswer`/`answer_resolution.py`, `FormField.options` is set to `("Yes", "No")` at discovery time for `CHECKBOX_ACKNOWLEDGEMENT` fields, and the type is added to `field_mapping.py`'s `_OPTION_BEARING_TYPES` (so `schema_matches()`'s drift comparison covers it consistently with every other choice-bearing type). This means `answer_resolution.py::_enforce_option_constraint()` — already generic over any field with a non-empty `question.options` — enforces the answer is exactly `"Yes"` or `"No"` with zero new code in that module. No special-casing needed anywhere in the LLM/answer-resolution layer; the existing option-set enforcement mechanism just works.

### D3. Consent-style checkboxes rely on the existing hard-exclusion boundary, not new logic

R4 is satisfied entirely by the classifier that already exists: `classify()` (`apps/auto_apply/llm/categories.py`) pattern-matches question text into `QuestionCategory.LEGAL_ATTESTATION` for phrasing like "I agree to the terms", and `HARD_EXCLUDED_CATEGORIES` already forces every such question to `needs_review=True` regardless of LLM confidence (`apps/auto_apply/llm/base.py:208-214`). Since a standalone checkbox's discovered `label` text becomes the `Question.text` classify() runs against (same as any other custom question, per `drafting.py:185-188`), a checkbox labeled "I agree to the Terms of Service" is *already* correctly routed to human review the moment it becomes a discoverable field at all (D1) — no new classification code required. This is the reason D1's fix is the prerequisite for R4, not a separate implementation unit.

### D4. Post-fill re-discovery is one bounded pass, not a loop, and only after the full initial fill completes

Re-running `_discover_schema()` mid-fill after every single field (to catch a reveal as early as possible) would multiply Playwright round-trips by the field count and risk redundant/partial re-discovery mid-fill. Instead, `_fill_answers()` completes its existing single pass over the known schema exactly as today, and *afterward*, `submit()` performs exactly one additional `_discover_schema()` call and diffs its field set against the schema used for filling. This bounds the added cost to one extra discovery pass per submission (not per field, not a retry loop), matches R5's "not an unbounded polling loop" constraint directly, and catches the common real-world case (a single "Other" reveal, or a handful of them from one page of answers) without pretending to handle arbitrarily deep reveal chains (a revealed field whose own answer reveals a further field) — that deeper case is not run-away polling, but genuinely open-ended, and is explicitly out of scope (see Scope Boundaries).

### D5. New-field detection reuses `GreenhouseFormSchemaMismatch`, not a new exception type

`tasks.py::_reason_code_for()` already maps `GreenhouseFormSchemaMismatch` to `AutoApplyDraft.ReasonCode.SCHEMA_MISMATCH` ("Unsupported form field"). A field appearing that was never in the answered schema is, from the caller's perspective, the same class of problem: "this form needs something the draft didn't account for." Reusing the existing exception/reason-code avoids adding a new `ReasonCode` enum value, a new `tasks.py` mapping branch, and a new user-facing message in `apps/web/views.py` for a case that is semantically the same failure family. The exception message names the specific newly-appeared field label(s) so the existing generic "Unsupported form field" UI copy still carries useful detail via `error_message`.

---

## Implementation Units

### U1. Discover standalone checkboxes as `CHECKBOX_ACKNOWLEDGEMENT` fields

**Goal:** A standalone checkbox with its own `<label>` (no enclosing `<fieldset>`) is discovered as a first-class schema field instead of being silently skipped.

**Requirements:** R1, R2

**Dependencies:** none

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/field_mapping.py`
- Modify: `apps/auto_apply/greenhouse_form/client.py`
- Modify: `apps/auto_apply/tests/test_greenhouse_form_client.py`
- New fixtures: `apps/auto_apply/tests/fixtures/greenhouse_standalone_checkbox_form.html`, `apps/auto_apply/tests/fixtures/greenhouse_standalone_checkbox_required_unfilled_form.html`

**Approach:** Add `CHECKBOX_ACKNOWLEDGEMENT = "checkbox_acknowledgement"` to `field_mapping.py`, include it in `SUPPORTED_FIELD_TYPES` and `_OPTION_BEARING_TYPES` (per D2). In `client.py`'s `_discover_schema()` main loop, where a checkbox control currently either becomes a `CHECKBOX_GROUP` (via `_checkbox_group_field()`) or is silently dropped via `continue` (client.py:495-509): when `_checkbox_group_field()` returns `None` (no enclosing fieldset), build a `FormField` directly from the already-resolved `label_text` and `control` for this branch — `field_type=CHECKBOX_ACKNOWLEDGEMENT`, `options=("Yes", "No")`, `required=self._is_required(control)`, `control_id=control.get_attribute("id") or ""`. Apply the same required-and-unsupported fail-closed check the rest of the loop already applies (client.py:536-540) — though for this type `is_supported` is always `True` once added to `SUPPORTED_FIELD_TYPES`, so R2 is satisfied structurally: the field is never silently missing again, and any future truly-unsupported checkbox variant still fails closed via the existing generic check.

**Patterns to follow:** The existing `CHECKBOX_GROUP` branch immediately above in the same loop (client.py:495-509) for control flow shape; `_checkbox_group_field()`'s own `required`/`label`/`control_id` extraction (client.py:675-727) for how a checkbox's required-ness and label are read.

**Test scenarios:**
- Happy path: `inspect()` on a fixture with one standalone required checkbox returns a schema containing a `CHECKBOX_ACKNOWLEDGEMENT` field with `options=("Yes", "No")` and `required=True`.
- Edge case: a standalone *non-required* checkbox is discovered as `required=False` and does not block submission when left unanswered.
- Regression: an existing `CHECKBOX_GROUP` fixture (`<fieldset><legend>` markup) is still discovered as `CHECKBOX_GROUP`, not misclassified as `CHECKBOX_ACKNOWLEDGEMENT` — confirms the fieldset-detection branch order is unaffected.
- Fail-closed guard (R2): a fixture where a required standalone checkbox is left unanswered by `submit()`'s answers dict raises (via the existing "answer for a field the page no longer renders" / blank-required path — verify current behavior in `_fill_answers()`/`_blocking_required_fields()` for a `CHECKBOX_ACKNOWLEDGEMENT` field with no matching answer, and add coverage for whichever path actually fires).

**Verification:** `apps.auto_apply.tests.test_greenhouse_form_client` passes in full, including new standalone-checkbox tests; full `apps.auto_apply` suite has no regressions.

---

### U2. Fill `CHECKBOX_ACKNOWLEDGEMENT` fields

**Goal:** `_fill_answers()` can check or uncheck a discovered standalone checkbox based on a resolved `"Yes"`/`"No"` answer.

**Requirements:** R3

**Dependencies:** U1

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/client.py`
- Modify: `apps/auto_apply/tests/test_greenhouse_form_client.py`

**Approach:** Add a `CHECKBOX_ACKNOWLEDGEMENT` branch to `_fill_answers()`'s field-type dispatch (client.py:867-904), alongside the existing `COMBOBOX_SELECT`/`CHECKBOX_GROUP` branches: `"Yes"` → `control.check()`, `"No"` → `control.uncheck()`. Because `_enforce_option_constraint()` (D2) already guarantees any value reaching this point is exactly `"Yes"` or `"No"`, no third branch/error path is needed here for an invalid value — mirror the existing pattern where option-constrained fields trust the upstream guarantee (e.g. `SINGLE_SELECT`'s `select_option()` call has no separate "value not in options" guard beyond the Playwright exception translation already in place).

**Patterns to follow:** The existing `CHECKBOX_GROUP`/`COMBOBOX_SELECT` dispatch branches immediately above (client.py:897-900).

**Test scenarios:**
- Happy path: `submit()` with answer `"Yes"` for a required standalone checkbox results in the checkbox being checked and the submission succeeding (using the U1 fixture).
- Happy path: answer `"No"` leaves the checkbox unchecked (verify via the control's checked state, not just no error).
- Integration: end-to-end `submit()` call (not just the isolated fill function) exercises the full discover → resolve → fill → confirm chain for a form whose only field is the standalone checkbox plus the existing baseline required fields (First Name, Email, Resume).

**Verification:** New tests pass; `apps.auto_apply.tests.test_greenhouse_form_client` full suite green.

---

### U3. Confirm answer-resolution routes consent-style checkboxes to human review

**Goal:** Verify and lock in (via test) that a standalone checkbox whose label matches existing `LEGAL_ATTESTATION` phrasing is never auto-answered by the LLM — it always resolves to `needs_review=True`, per the existing hard-exclusion boundary.

**Requirements:** R4

**Dependencies:** U1 (a checkbox must be discoverable before it can reach the answer-resolution layer at all)

**Files:**
- Modify: `apps/auto_apply/tests/test_answer_resolution.py` (or the closest existing test module covering `resolve_field_answers`/`_enforce_option_constraint` — confirm exact path during implementation)
- Modify: `apps/auto_apply/tests/test_drafting.py` (or equivalent, for the end-to-end draft-time path)

**Approach:** This unit is primarily a **verification** unit, not new production code (per D3, the existing `classify()`/`HARD_EXCLUDED_CATEGORIES` mechanism already handles this correctly once U1 makes the checkbox discoverable). Add targeted tests proving the behavior: a `Question` with `text="I agree to the Terms of Service"` classifies as `LEGAL_ATTESTATION` and is routed to `needs_review=True` without ever calling the injected `llm_client` (assert the fake/mock LLM client's `infer()` was not called for that question, consistent with existing hard-exclusion test patterns in the LLM test suite). Also add an end-to-end `drafting.draft_for()` test confirming a standalone required checkbox with attestation-style copy lands in `answers_payload` as a blank `needs_review=True` placeholder (not excluding the whole draft, matching the existing custom-question-unanswerable behavior at `drafting.py:194-229` — a required checkbox is not a FILE-type field, so it does not hit the one exclude-the-draft exception).

**Patterns to follow:** Existing `HARD_EXCLUDED_CATEGORIES` test coverage in the LLM test suite (locate via existing tests asserting `WORK_AUTHORIZATION`/`LEGAL_ATTESTATION` never reach `infer()`) and the existing blank-needs_review-placeholder tests in `drafting.py`'s own test module.

**Test scenarios:**
- Happy path: a standalone checkbox labeled with existing `LEGAL_ATTESTATION`-pattern text resolves to `needs_review=True`, LLM client never called for it.
- Regression: a standalone checkbox with plainly generic (non-legal-sounding) label text is still handled correctly (either LLM-resolved and constrained to `"Yes"`/`"No"` via `_enforce_option_constraint`, or `needs_review` if the LLM can't confidently answer) — proves U1/U2's option-constraint plumbing (D2) works end-to-end for the non-hard-excluded case too, not only the attestation case.
- Integration: full `draft_for()` call with a fixture containing a required attestation-style checkbox produces a `DRAFTED` (not `EXCLUDED`) draft with the checkbox as a blank `needs_review` placeholder in `answers_payload`.

**Verification:** New/modified tests pass; confirms R4 is satisfied by existing infrastructure plus U1's discovery fix, with no new classification logic introduced.

---

### U4. One bounded post-fill re-discovery pass; fail closed on newly-revealed required fields

**Goal:** After `_fill_answers()` completes its single pass, `submit()` performs exactly one additional discovery pass to detect any required field that appeared as a side effect of the answers just filled, and fails closed with a clear error if one is found and unanswered.

**Requirements:** R5, R6, R7

**Dependencies:** none (independent of U1-U3; both gaps live in the same file but touch different code paths)

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/client.py`
- Modify: `apps/auto_apply/tests/test_greenhouse_form_client.py`
- New fixture: `apps/auto_apply/tests/fixtures/greenhouse_reveals_required_field_on_select_form.html`

**Approach:** In `submit()`, immediately after the existing `self._fill_answers(page, schema_now, answers)` call (client.py:350) and before the success-confirmation poll begins, add one `re_discovered = self._discover_schema(page, job_url)` call. Compute the set of fields in `re_discovered` whose `label` was not present in `schema_now` (the schema that was actually filled) — these are candidate newly-revealed fields. Among those, filter to `required=True`. If any remain, raise `GreenhouseFormSchemaMismatch` naming the revealed field label(s) (per D5) before attempting to click Submit — this is a pre-submit fail-closed check, not a post-submit failure, so no partial/ambiguous submission state is possible. If the newly-revealed set is empty (the common case — R7), proceed exactly as today with zero behavior change beyond the one extra `_discover_schema()` call's cost.

**Patterns to follow:** `schema_matches()`'s existing field-set diffing logic in `field_mapping.py` for the general shape of comparing two `FormSchema`s by label (though this unit needs asymmetric "what's new" diffing, not `schema_matches()`'s symmetric equality check — do not reuse `schema_matches()` directly, write a small local diff instead).

**Test scenarios:**
- Happy path (R7): a form with no revealed fields — `submit()` behaves identically to before this change (same success path, same field count filled, no new errors).
- Reveal-with-required-field (R5, R6): a fixture where selecting a combobox/dropdown value reveals a new required text field not in the original schema and not in `answers` — `submit()` raises `GreenhouseFormSchemaMismatch` naming the revealed field, and does not click the real Submit button (verify via a spy/assertion that the submit control was never triggered, or that the page never navigated/confirmed).
- Reveal-without-required-field: a revealed field that is *not* required does not block submission — the bounded re-discovery pass only fails closed on required-and-unanswered new fields, matching the existing required-field-only fail-closed posture used everywhere else in this file.
- Boundary: exactly one extra `_discover_schema()` call happens regardless of how many fields were filled or revealed — assert (e.g., via a call-count spy in a test double) that this is O(1) additional discovery work per submission, not O(fields filled), proving R5's "not a polling loop" constraint.

**Verification:** New tests pass; full `apps.auto_apply` suite green; no regression in existing submit-path tests' timing/call-count assumptions.

---

## Risks & Open Questions

- The exact real-world markup/phrasing for standalone Greenhouse consent checkboxes is unconfirmed (no live capture exists yet for this specific pattern, unlike the verification-interstitial and education-API fixes on this branch, which were grounded in captured real evidence). This plan's fixtures are constructed from Greenhouse's documented/observed markup conventions elsewhere in this codebase, not a live capture — flagged per this codebase's own established discipline of preferring live evidence before finalizing DOM-dependent selectors. If a real board's standalone-checkbox markup differs materially (e.g., no `<label>` at all, relying on adjacent text), this plan's discovery logic may need a second pass, mirroring the `aria-labelledby`-only fields precedent (client.py:546-620).
- U4's single-pass re-discovery does not handle a *chain* of reveals (a revealed field whose own required-ness or presence depends on yet another answer). This is explicitly deferred (Scope Boundaries) rather than solved with an unbounded loop, per R5's own constraint.
