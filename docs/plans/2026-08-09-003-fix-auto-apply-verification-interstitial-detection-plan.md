---
title: Fix Greenhouse verification-interstitial detection missing the confirmed multi-box code shape
type: fix
status: active
date: 2026-08-09
---

# Fix Greenhouse Verification-Interstitial Detection Missing the Confirmed Multi-Box Code Shape

## Summary

The Greenhouse email-verification-code automation (IMAP app-password based, built in PR #48 and hardened in two follow-up fixes on 2026-08-06) is fully implemented: `EmailInboxCredential`, the IMAP `EmailCodeProvider`, and `GreenhouseFormClient`'s three-way `_confirm_success()` classifier all exist and are wired into `submit_auto_apply_draft`. The most recent failed draft (`AutoApplyDraft` id 70, user `yashverma10019@gmail.com`) proves the feature is *not* working end-to-end for at least one real posting: the user received the verification email, but the draft still failed with the generic, pre-verification-feature error `"No post-submit success signal found at ..."` (`reason_code=submission_failed`) rather than any of the verification-specific outcomes.

A captured debug artifact from the exact failure timestamp (`media/auto_apply_debug/1786259666-c46a2165-a11y.yaml`) shows the real interstitial rendered on screen at the moment the draft gave up: a group whose accessible text is *"A verification code was sent to yashverma10019@gmail.com. To submit your application, enter the 8-character code to confirm you're a human. Security code"*, containing 8 separate single-character textboxes.

This proves `GreenhouseFormClient._verification_interstitial_detected()` (`apps/auto_apply/greenhouse_form/client.py:1278`) returned `False` while the interstitial was literally on screen, causing `_confirm_success()` to poll out its budget and raise the old generic `GreenhouseFormSubmissionFailed` instead of routing into the verification-code flow at all — `build_email_code_provider()` was never even asked for a code.

**Root cause:** `_verification_interstitial_detected()` requires two independent signals — confirming copy (`_VERIFICATION_TEXT_PATTERNS`, which the real page's copy does satisfy: "verification code" is a literal substring) **and** a code-entry control matching `_VERIFICATION_CODE_INPUT_SELECTOR`. That selector's own code comment documents it as an *unverified guess* (`autocomplete="one-time-code"`, `inputmode="numeric"`, or name/id/placeholder containing "code"), assembled before any real interstitial was captured. Separately, `_fill_verification_code()` already has a **confirmed** live selector for the real multi-box shape — `_VERIFICATION_CODE_BOX_SELECTOR = 'input[maxlength="1"]'` — captured from a real accessibility-tree dump, per the comment at client.py:149-158. That confirmed selector was never added to the detection-side `_VERIFICATION_CODE_INPUT_SELECTOR`. If the real per-character boxes don't happen to carry `inputmode="numeric"` or a code-bearing `name`/`id`/`placeholder` (plausible for a componentized OTP widget using e.g. `data-*` attributes or bare unstyled inputs), detection's `code_input.count()` is 0 and the whole classifier misses the interstitial — even though the *fill* logic, which already trusts the confirmed selector, would have handled it correctly had detection let it run.

---

## Requirements

- R1. `_verification_interstitial_detected()` recognizes Greenhouse's real multi-box verification interstitial (the confirmed `input[maxlength="1"]` shape), not only the unverified single-input guess.
- R2. The fix does not introduce new false positives — pages with `maxlength="1"` inputs unrelated to verification (e.g. address zip/postal code splits, if any exist) must still require the confirming-copy signal before being classified as the interstitial (existing two-signal design is preserved, not weakened to a single signal).
- R3. Regression coverage proves the multi-box shape alone (previously silently missed) is now detected, alongside the existing single-input shape.

## Scope Boundaries

- Not in scope: rebuilding the IMAP/email-verification feature — it already exists and is architecturally sound (`apps/auto_apply/email_verification/`, `apps/accounts/models.py::EmailInboxCredential`, `apps/auto_apply/tasks.py`). This plan fixes one detection gap in an already-shipped feature.
- Not in scope: re-driving `AutoApplyDraft` id 70 or any other historically failed draft — out of scope for this code fix; the user can re-trigger auto-apply once the fix ships.
- Not in scope: broadening `_VERIFICATION_TEXT_PATTERNS` — the real page's copy already matches "verification code" per the captured artifact; no evidence of a text-matching gap.
- Not in scope: the confirmation_timeout/deadline budget math (`_submit_budget_seconds`, `_SWEEP_SAFETY_MARGIN_SECONDS`) — already correctly threaded per the 2026-08-06 plan; not implicated by this failure (the poll ran and *did* see the interstitial's copy in principle, it just failed the control-selector half of the check).

---

## Implementation Units

### U1. Add the confirmed multi-box shape to the detection selector

**Goal:** `_verification_interstitial_detected()` recognizes the interstitial when the confirmed `input[maxlength="1"]` box shape is present, not only the unverified single-input guess.

**Requirements:** R1, R2, R3

**Dependencies:** none

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/client.py`
- Modify: `apps/auto_apply/tests/test_greenhouse_form_client.py`
- New fixture (or extend an existing verification fixture): `apps/auto_apply/tests/fixtures/greenhouse_verification_multibox_form.html`

**Approach:** In `_verification_interstitial_detected()` (client.py:1278), check for a code-entry control using **both** the existing `_VERIFICATION_CODE_INPUT_SELECTOR` and the confirmed `_VERIFICATION_CODE_BOX_SELECTOR` (`input[maxlength="1"]`), requiring only that at least 2 such boxes are present when using the multi-box path (a single stray `maxlength="1"` input elsewhere on the page — e.g. a 1-character initials field — should not alone satisfy the control-signal; the existing `_fill_verification_code()` logic already treats `box_count >= 2` as the multi-box case, so mirror that threshold here for consistency). The confirming-copy check (`_VERIFICATION_TEXT_PATTERNS`) stays required either way — R2's false-positive guard is unchanged, only the control-signal half gains a second recognized shape. Update the stale code comment above `_VERIFICATION_CODE_INPUT_SELECTOR` (client.py:116-137) to reflect that the multi-box shape is now a confirmed, explicitly-checked signal rather than leaving it looking unaddressed.

**Patterns to follow:** The existing multi-box vs. single-input branching already implemented in `_fill_verification_code()` (client.py ~1231-1275), which is the confirmed reference for how the two shapes coexist.

**Test scenarios:**
- Happy path: a fixture rendering the confirmed real shape (confirming copy + 8 `input[maxlength="1"]` boxes, no single input matching `_VERIFICATION_CODE_INPUT_SELECTOR`) is detected as the interstitial (regression test for this exact bug — this is the scenario that silently failed for draft 70).
- Existing single-input shape (already covered by current tests) continues to be detected — no regression.
- False-positive guard: confirming copy present but zero code-entry controls of either shape → not detected.
- False-positive guard: a single stray `input[maxlength="1"]` (e.g., an unrelated 1-character field) with confirming copy absent → not detected (copy signal still required).
- Success page whose copy happens to overlap verification phrasing (e.g., "check your email for next steps") still loses the tie to the success signal per `_confirm_success()`'s existing success-first ordering — confirm this ordering test still passes unchanged.

**Verification:** `apps.auto_apply.tests.test_greenhouse_form_client` passes in full, including the new multi-box detection test; full `apps.auto_apply` suite has no regressions.

---

## Risks & Open Questions

- The exact real HTML attributes of the live multi-box inputs (beyond `maxlength="1"`, which is confirmed) remain uncaptured — this fix targets the confirmed structural signal rather than guessing further attributes, so it should hold even if other attributes differ from the original assumption.
- If a future Greenhouse board renders the interstitial with neither the single-input nor the multi-box shape, this fix would not catch it — no evidence of that today; revisit if another draft fails the same way after this ships.
