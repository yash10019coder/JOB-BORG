---
title: "Greenhouse verification-code click lands on a stale same-text submit button"
date: 2026-10-02
category: logic-errors
module: auto_apply
problem_type: logic_error
component: background_job
symptoms:
  - "Verification code is looked up and entered correctly, but the page stays on the interstitial forever with no error"
  - "AutoApplyDraft repeatedly fails with reason_code=submission_unconfirmed / error message \"Verification code entered but application success not confirmed\""
  - "Failure reproduces identically across multiple live submission attempts on one specific employer's Greenhouse board (Canonical)"
  - "No exception from Greenhouse itself -- the click appears to succeed, but nothing on the page ever changes"
root_cause: logic_error
resolution_type: code_fix
severity: critical
related_components:
  - greenhouse_form_client
  - email_verification
tags: [greenhouse, playwright, selector-ambiguity, verification-code, auto-apply, dom-scoping]
---

# Greenhouse verification-code click lands on a stale same-text submit button

## Problem

Auto-apply submission to Canonical's Greenhouse job board (job 146031) repeatedly failed after the email-verification code was correctly entered: the page appeared permanently stuck on the "enter verification code" interstitial, with no error and no success signal. Five real, live submission attempts against the real employer board failed before the actual root cause was found, including two attempts that reached the genuinely ambiguous "code submitted, success unconfirmed" state -- the kind of outcome that risks a duplicate real application if retried carelessly.

## Symptoms

- `GreenhouseFormSubmissionUnconfirmed: Verification code entered but application success not confirmed` raised repeatedly (3+ times) on the same employer board, after the code had been correctly looked up via IMAP and typed.
- No error ever surfaced from Greenhouse itself -- the page simply never advanced past the interstitial.
- A first diagnostic capture of the page's rendered text showed only the static job-description header, making it look like the apply flow hadn't even started (a red herring from a shallow capture, not the real bug -- see "What Didn't Work").
- Every earlier stage of the pipeline worked correctly: form fill, initial submit, schema-drift check, IMAP code lookup. Only the post-code "click Submit/Verify" step silently did nothing.

## What Didn't Work

Several real, distinct bugs were found and fixed along the way in the same debugging session -- each was necessary but not sufficient, since the submission kept failing with a *different* symptom after each fix:

- **CRLF/newline normalization in textarea fill-verification** (`apps/auto_apply/greenhouse_form/client.py`'s `TEXT`/`TEXTAREA` fill-and-verify step) -- a real bug (browsers normalize `\r\n` differently per control type than Playwright's literal-string `to_have_value` assertion expected), fixed, but orthogonal to the verification freeze.
- **Discipline/School/Degree combobox scroll-vs-type-to-filter mismatch** -- these education-API-backed comboboxes require scrolling to reveal options rather than typing to filter them, unlike the react-select-style Country/Location fields. Also a real, separate bug, fixed, but unrelated to the freeze.
- **Reclassifying an ambiguous outcome from `CODE_REJECTED` to `GreenhouseFormSubmissionUnconfirmed`** -- necessary on its own (the old classification silently bypassed the app's own `SUBMISSION_UNCONFIRMED` duplicate-application safety guard in `apps/web/views.py`'s `trigger_auto_apply`, meaning a careless retry could have double-submitted a real application to a real employer) but did not stop the underlying freeze from recurring.
- **Restarting the stale Celery `worker` process** -- `celery worker` has no autoreload (unlike Django's `runserver`), so it had been running 20+ hours without picking up any of the above fixes. Necessary to make the other fixes effective at all, but still not sufficient on its own -- the verification step kept freezing afterward.
- **First diagnostic logging attempt** (`page.locator("body").inner_text()[:2000]`) captured only the first 2000 characters of the page, which on this employer's board is the long, unchanging static job-description header rendered at the top of the page through the *entire* flow (form, verification, and success states all render below it, on the same URL). This made it look like nothing was happening at all. Corrected to capture the *last* 3000 characters instead, which immediately revealed the real state: the page was still showing the verification-code prompt, with the "Submit application" button right there, unclicked in effect.
- **(session history) An earlier session's "prefer a button whose text says Verify" fix** had already solved a near-identical stale-button collision for a *different* employer -- that fix taught the code to prefer a button labeled "Verify" over a generic "Submit"-labeled one, specifically to avoid clicking a stale pre-verification submit button left in the DOM by an overlay-style interstitial. It did nothing for Canonical, because Canonical's interstitial button is *also* labeled "Submit application" -- identical to the stale button, not "Verify". This is the third time this exact class of bug (ambiguous button-text selection against Greenhouse's live DOM) has appeared in this file; see "Related Issues".
- **(session history) A real native-OTP-widget keystroke bug** (`.fill()` not firing the keydown/keyup events some Greenhouse OTP widgets need to enable their submit button) was fixed in an earlier session and is unrelated to this bug, but is the same general lesson: assumptions about a live third-party widget's behavior need verification against the real DOM, not just a fixture.

## Solution

Added `_find_verification_submit_button(page)` to `apps/auto_apply/greenhouse_form/client.py`, scoping the submit-button search to the verification code input's own enclosing `<form>` instead of searching the whole page by button text.

```python
# Before: page-wide text search, ambiguous whenever two buttons share text
verify_button = page.locator("button:has-text('Verify')").first
submit_button = (
    verify_button
    if verify_button.count() > 0
    else page.locator(
        "button[type='submit'], input[type='submit'], button:has-text('Submit')"
    ).first
)

# After: scope to the code input's own enclosing <form> first
@staticmethod
def _find_verification_submit_button(page):
    try:
        code_input = page.locator(_VERIFICATION_CODE_INPUT_SELECTOR).first
        if code_input.count() == 0:
            code_input = page.locator(_VERIFICATION_CODE_BOX_SELECTOR).first
        if code_input.count() > 0:
            form_scope = code_input.locator("xpath=ancestor::form[1]").first
            if form_scope.count() > 0:
                verify_in_form = form_scope.locator("button:has-text('Verify')").first
                if verify_in_form.count() > 0:
                    return verify_in_form
                submit_in_form = form_scope.locator(
                    "button[type='submit'], input[type='submit'], button:has-text('Submit')"
                ).first
                if submit_in_form.count() > 0:
                    return submit_in_form
    except Exception:
        pass  # fall through to the unscoped heuristic below

    # No enclosing <form> found (or anything above raised) -- fall back
    # to the old page-wide heuristic rather than raising.
    verify_button = page.locator("button:has-text('Verify')").first
    if verify_button.count() > 0:
        return verify_button
    return page.locator(
        "button[type='submit'], input[type='submit'], button:has-text('Submit')"
    ).first
```

The `try/except` fallback is deliberate, not defensive filler: several pre-existing unit tests drive `_confirm_success` with a fully-mocked `page` object that doesn't model a real Playwright locator chain (e.g. `mock_page.locator.return_value.first.count.return_value = 0`, shared across every selector string). Without the fallback, those mocks raised `TypeError` (comparing a `MagicMock` to `0`) instead of exercising the intended fallback path. Falling through to the old heuristic on any error keeps those tests passing unchanged while still being the right production behavior: DOM-scoping is a precision improvement, not something a real submission should ever hard-fail on.

Regression test added: `apps/auto_apply/tests/fixtures/greenhouse_verification_same_text_as_stale_submit_form.html` (a fixture where both the stale pre-verification button and the interstitial's own button are labeled identically, "Submit application") + `test_submit_verification_button_same_text_as_stale_button_still_clicks_right_one` in `apps/auto_apply/tests/test_greenhouse_form_client.py`. Confirmed to reproduce the exact production error message when the fix is reverted, and to pass with it applied.

Verified live in production: `AutoApplyDraft` #271 reached `status=applied`, with `JobApplication` #7 created, for the real job posting at `job-boards.greenhouse.io/canonical/jobs/5703396`.

## Why This Works

Canonical's Greenhouse board renders the post-submit email-verification interstitial's own "Submit" button with the exact same visible text -- **"Submit application"** -- as the original, now-stale application-form submit button that Greenhouse leaves in the DOM when the interstitial renders as an overlay rather than a full-page replacement. An earlier, different real production fix had already taught this code to prefer a button whose text says "Verify" to disambiguate exactly this kind of stale-button collision -- but that heuristic is worthless here, because *neither* button says "Verify." A page-wide selector matching on generic "Submit" text is fundamentally ambiguous once two buttons share that text, and Playwright's `.first` will silently pick whichever one happens to come first in DOM order -- which, on this board, was the dead one. Only DOM-structural scoping -- which `<form>` the actual code input lives in -- disambiguates correctly regardless of what either button is labeled.

## Prevention

- Never resolve a form-submission control by page-wide text search alone when multiple forms can coexist in the DOM. Overlay-style interstitials leaving a stale button behind is a recurring, known Greenhouse pattern in this codebase (this is the third occurrence in `apps/auto_apply/greenhouse_form/client.py` alone -- see Related Issues). Scope to the relevant form/container first, and treat text matching as a fallback signal, not the primary one.
- When diagnosing a "stuck forever with no error" browser-automation symptom, add a safe diagnostic capture: confirm first that whatever you log structurally cannot include sensitive input values (`Locator.inner_text()` never includes form control values, unlike an accessibility-tree dump or a screenshot -- both of which stay suppressed on this code path specifically to avoid ever writing a live OTP to disk), and capture enough content -- especially the *end* of a long page, not just the first N characters -- to see the actual dynamic state rather than static boilerplate that happens to render first.
- After editing code imported by a Celery worker (or any long-running process without autoreload), restart that process (`docker compose restart worker beat` in this repo) before concluding a fix didn't work. Django's `runserver` autoreloads; `celery worker` does not -- a correct fix on disk can still appear to fail live for hours.
- Any exception path that only means "we don't know if the action already took effect" must raise an ambiguous-outcome signal (`GreenhouseFormSubmissionUnconfirmed`), never a "known-safe-to-retry" rejection type -- otherwise a duplicate-submission guard elsewhere in the app (`trigger_auto_apply`'s `SUBMISSION_UNCONFIRMED` check) can be silently bypassed, and a retry can genuinely double-submit a real application to a real employer.
- (session history) `press_sequentially()`, not `.fill()`, is the standing approach for OTP/code-entry boxes generally in this file -- some native Greenhouse widgets only enable their submit button in response to real keystroke events, which `.fill()` does not dispatch.

## Related Issues

- `docs/solutions/logic-errors/greenhouse-combobox-option-discovery-race-and-pagination.md` -- a different symptom and code path in the same file, but the same underlying institutional lesson, now independently confirmed a second time: an assumption about how a live, third-party Greenhouse SPA behaves (there, async-rendered option lists; here, button-text uniqueness) broke in production and had to be verified against a real board, not just a fixture. That doc's existing "verify live, not just against fixtures" prevention guidance applies directly here.
- (session history) An earlier session (branch `fix/auto-apply-remote-resume-storage`, 2026-09-30) fixed the *first* instance of this exact bug class for a different employer, by adding the "prefer a Verify-labeled button" heuristic that this fix's own code still falls back to. That fix was necessary but not general enough -- it assumed at least one of the two colliding buttons would say "Verify," which doesn't hold on every board.
- GitHub issue #51 ("Post-code success check only waits once, can misclassify slow success as CODE_REJECTED"), fixed by an earlier PR (`fix/auto-apply-poll-after-verification-code`) that added polling for confirmation after the code is submitted. That fix is adjacent (same post-verification-code confirmation flow) but distinct -- it addressed insufficient *waiting*, not wrong *button selection*. Both fixes live in the same `_confirm_success` area of `client.py`.
- GitHub issue #59 ("Disabled-Submit diagnostic checks button state with no settle window") touches the same submit-button-detection code area and may be worth a second look given this fix's changes to button resolution.
