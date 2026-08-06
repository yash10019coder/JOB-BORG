---
title: Fix Greenhouse Field/Button Discovery Gaps and Historical Auto-Apply Failures
type: fix
status: completed
date: 2026-08-06
---

# Fix Greenhouse Field/Button Discovery Gaps and Historical Auto-Apply Failures

## Summary

Investigation of debug artifacts (`media/auto_apply_debug/`, 5 screenshot+accessibility-tree pairs) and historical `AutoApplyDraft` records confirms the user's report: `GreenhouseFormClient`'s field-discovery logic in `apps/auto_apply/greenhouse_form/client.py` structurally misses an entire class of Greenhouse form fields, and this is the dominant cause of both unfilled fields and historical submission failures.

**Root cause (confirmed via accessibility-tree evidence):** `_discover_schema()` (client.py:450) starts discovery from `page.locator("form label")` — i.e. it only finds fields whose accessible name comes from a real `<label>` element. But every custom Greenhouse question observed in the captured artifacts (Gender, Hispanic/Latino, Veteran Status, Disability Status, custom Yes/No eligibility questions, Location (City), Phone Country) renders as a bare `text:` node immediately followed by a `combobox` node with its accessible name supplied via `aria-labelledby` pointing at that plain text node — not a `<label>` element at all. These fields never enter the `labels` loop; they are invisible to discovery, not merely misclassified.

This directly explains the two largest historical failure buckets:
- `submission_failed` (10 rows): 8 of 10 have `error_message = "No post-submit success signal found..."` — consistent with Submit staying disabled because undiscovered required comboboxes were never filled, making the click a silent no-op.
- `unanswerable_required` (15 rows, the single largest bucket): repeatedly cites "Phone" as unanswerable even though users have a phone number on file, consistent with the phone field's label sitting ambiguously inside a `group "Phone"` alongside a `combobox "Country"`.

A second, independent bug was found in the email-verification interstitial: `1785860486-8e3b8ab2-a11y.yaml` shows the real verification code control rendered as **8 separate single-character `<textbox>` elements**, not the single input `_VERIFICATION_CODE_INPUT_SELECTOR` (client.py:123-131) assumes. The current fill (`code_input = page.locator(...).first`, client.py:922-923) only types into the first character box.

A third, smaller bug: `_click_submit` (client.py:833-837) never checks whether the Submit button is disabled before clicking, so a click blocked by an unfilled required field surfaces only as the generic, undiagnostic "No post-submit success signal found" — with no indication of which field was the actual blocker.

## Requirements

- R1. `_discover_schema()` finds fields whose accessible name is supplied via `aria-labelledby`/`aria-label` pointing at a non-`<label>` element (the react-select-style combobox pattern used throughout Greenhouse's custom/EEO questions), not only fields wrapped by or pointing at a real `<label>`.
- R2. Newly-discovered `aria-labelledby`-style combobox fields are classified and filled through the existing `COMBOBOX_SELECT` path (`_extract_options`/`_fill_combobox`) with no separate fill mechanism required.
- R3. The email-verification code fill correctly handles a multi-box (N single-character input) code entry UI in addition to the existing single-input case, without regressing the single-input path.
- R4. Before clicking Submit, the client detects whether the button is disabled and, if so, raises a specific, diagnostic error identifying that submission was blocked by an unfilled/invalid required field — rather than falling through to the generic "no success signal" failure.
- R5. No regression to existing field discovery, classification, or fill behavior for fields that already work (real `<label>`-driven text/select/checkbox/file fields).

## Scope Boundaries

- Not in scope: the "Phone" unanswerable-required pattern's root cause outside `client.py` (the answer-mapping/drafting layer that decides a field is unanswerable) — flagged as a likely consequence of R1 (once the Phone field is correctly discovered as a single field rather than an ambiguous group, the drafting layer should be able to answer it), but no drafting-layer code change is planned here. If R1 alone does not resolve it, that is separate follow-up work, not blocking for this plan.
- Not in scope: general redesign of the discovery algorithm (e.g., switching to full accessibility-tree traversal). The fix is additive — broaden the set of "candidate label sources" feeding the existing per-label resolution/classification pipeline, not a rewrite.
- Not in scope: non-Greenhouse ATS platforms; only Greenhouse-shaped DOM patterns observed in the captured evidence are addressed.
- Not in scope: `sending_timeout` (3 rows) and `form_load_failed`/`schema_mismatch` (5 rows combined) historical buckets — no evidence in the captured artifacts ties these to discovery bugs; they are pre-existing, already-instrumented failure modes with their own reason codes, and are left as-is.

## Key Technical Decisions

### D1. Extend the label-source enumeration, don't replace `_discover_schema`'s pipeline

`_discover_schema` today does: enumerate `form label` elements -> resolve each to a control -> classify -> extract options/build field spec. The fix adds a second enumeration pass for controls whose accessible name comes from `aria-labelledby`/`aria-label` referencing a **non-`<label>`** element (matching Greenhouse's rendered pattern: a bare text node followed by `[role=combobox]` or `[role=group]`), producing the same intermediate "label text + resolved control" shape the existing per-label loop already consumes. This keeps `_classify_field_type`/`_extract_options`/`_fill_combobox` untouched — only the *set of fields discovered* grows. De-duplicate against label-based discovery by control identity (not text) so a field already found via a real `<label>` isn't double-counted.

### D2. Multi-box verification code: detect and distribute, don't guess a new selector

Rather than replacing `_VERIFICATION_CODE_INPUT_SELECTOR` with an assumption about box count, detect at fill-time: query for the existing single-input selector first (preserves the working case); if it doesn't resolve to exactly one fillable input, look for a set of sibling single-character `<input>`/`<textbox>` elements inside the same verification container and distribute one character of the code to each in DOM order. Falls back to raising `GreenhouseFormVerificationFailed(outcome=CODE_REJECTED)` if neither shape matches, rather than guessing.

### D3. Disabled-submit detection is a pre-click check, not a new success-signal branch

Add an `is_disabled()` check on the resolved submit button immediately before the click. If disabled, raise `GreenhouseFormSubmissionFailed` with a message naming the still-invalid/empty required fields (cheap to compute: already-tracked schema knows which fields are required; re-check their current filled/valid state). This is diagnostic only — it does not change what "success" means, and existing success-signal detection is untouched.

## Files

- Modify: `apps/auto_apply/greenhouse_form/client.py` — `_discover_schema` (add `aria-labelledby`/`aria-label` candidate enumeration + de-dup), verification code fill (multi-box distribution), `_click_submit` (disabled-check + diagnostic error).
- Modify: `apps/auto_apply/greenhouse_form/tests/test_greenhouse_form_client.py` (or wherever the existing client test file lives — confirm exact path during implementation) — new fixtures and test cases below.
- New fixtures: an `aria-labelledby`-only combobox HTML fixture (mirroring the real Gender/Veteran/Disability/Location pattern), a multi-box verification-code HTML fixture, a disabled-submit-button HTML fixture.

## Test Scenarios

- `aria-labelledby` combobox with no `<label>` element is discovered, classified `COMBOBOX_SELECT`, options extracted, and fillable — the direct regression test for R1/R2.
- A field discoverable via both a real `<label>` and matching `aria-labelledby` structure is only counted once (de-dup regression guard).
- Existing `<label>`-driven text/select/checkbox/file fixtures still discover and fill identically (R5 regression guard — run full existing suite).
- Verification code fill: single-input case unchanged (existing behavior preserved); new multi-box fixture (e.g. 8 single-character inputs) receives one character each in order and submits correctly.
- Verification fill falls back to `CODE_REJECTED` cleanly when neither single-input nor multi-box shape is found (no exception escapes uncaught).
- Submit click on a fixture with a disabled submit button (simulating an unfilled required field) raises a diagnostic `GreenhouseFormSubmissionFailed` naming the offending field, instead of proceeding to the generic no-success-signal path.
- Submit click on a fixture with an enabled submit button proceeds exactly as before (no behavior change when nothing is disabled).

## Verification

- Full `apps.auto_apply` test suite green, including all new fixtures above.
- Live re-run (or fixture-based reproduction) against the two Alpaca postings that produced `"No matching option for '' found in combobox field 'Location (City)'"` and the 8/10 "No post-submit success signal" `submission_failed` rows — confirm the fields are now discovered/filled and the submit proceeds past the previously-blocked point.
- Spot-check the captured `1785860486-8e3b8ab2-a11y.yaml` multi-box verification scenario against the new fill logic (fixture reproduction, since replaying the live email is not repeatable).

## Risks

- The `aria-labelledby`-only pattern was observed consistently across all 5 captured artifacts but is still a sample of one ATS vendor's rendering at one point in time; Greenhouse could change this markup without notice (same risk class already accepted for the existing `_confirmation_text_patterns`/interstitial-detection selectors elsewhere in this file).
- Fixing R1 may surface previously-hidden downstream issues in the answer-mapping/drafting layer (e.g., the Phone group) that are explicitly out of scope here — expect a possible follow-up plan if `unanswerable_required` doesn't drop after this fix ships.
