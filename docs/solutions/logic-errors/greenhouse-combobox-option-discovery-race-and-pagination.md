---
title: "Greenhouse combobox option discovery: async-render race and remote pagination"
date: 2026-08-09
category: logic-errors
module: auto_apply
problem_type: logic_error
component: assistant
symptoms:
  - "Combobox fields like Degree returned 0 discovered options despite having real choices"
  - "School combobox capped at ~100 discovered options out of 2,466 available on the live board"
  - "A valid LLM-generated answer could be wrongly rejected as INVALID_OPTION because the discovered options snapshot was incomplete"
root_cause: async_timing
resolution_type: code_fix
severity: high
related_components:
  - background_job
tags: [greenhouse, playwright, combobox, pagination, async-race, option-discovery, react-select]
---

# Greenhouse combobox option discovery: async-render race and remote pagination

## Problem

`GreenhouseFormClient._extract_options()` (`apps/auto_apply/greenhouse_form/client.py`, `COMBOBOX_SELECT` branch) assumed that once a combobox's listbox opens, every real `<option>` is already present in the DOM and safe to read immediately. On a real Greenhouse job posting, that assumption was false in two independent ways for the "School" and "Degree" fields: options can render *asynchronously* after the listbox container mounts, and a large option list (School: 2,466 entries) can be *remotely paginated*, loading only its first page on a bare open.

## Symptoms

- "For input fields Degree and School do not have options to enter even though they are in a dropdown category." — `_extract_options()` returned 0 options for both fields.
- After fixing the above: "although we are getting inputs but still not all inputs for school like we are getting only top 20-30 schools starting with a only. I think there is some paginated request calling or something for them because of which it's stalled" — the user correctly diagnosed the mechanism (pagination) ahead of investigation.
- Downstream impact: a same-session, previously-shipped feature (`answer_resolution._enforce_option_constraint()`) rejects any LLM answer that doesn't exactly match the field's discovered `options` tuple. An incomplete discovery-time snapshot meant a perfectly valid real answer (e.g. an applicant's real school, alphabetically past whatever page happened to load) would be wrongly treated as unanswerable, even though the send-time fill path could have found and selected it correctly.

## What Didn't Work

**"Wrong listbox matched" hypothesis (bug 1, invalidated):** Suspected `page.get_by_role("listbox").first` was matching a different, always-mounted-but-invisible listbox elsewhere on the page (an international-telephone-input country-code picker, present in the DOM at 0×0 size even before any interaction). Directly disproven: a full accessibility-tree snapshot taken while the real combobox was open showed only one listbox node — Playwright's role-based query already excludes zero-size elements from the accessibility tree, so this was never the actual mechanism. Ruled out with snapshot evidence before forming the next hypothesis.

**Two-step wait: container-visible, then option-visible (bug 1, refined not discarded):** The first real fix waited for the listbox *container* to become visible (1s timeout), then for an *option* to become visible (5s timeout). This failed against a synthetic regression fixture reproducing the async-render race:

```
Locator.wait_for: Timeout 1000ms exceeded ... 11 × locator resolved to hidden <ul role="listbox">
```

A childless `<ul role="listbox">` can itself fail Playwright's "visible" check — it collapses to zero layout size with no content — so the container-visibility wait timed out before ever reaching the option-wait step. The diagnosis (wait for real option content, not just container mount) was correct; only the two-step implementation was wrong. Refined to a single wait, scoped directly to an option through the listbox.

**Programmatic `scrollTop` assignment (bug 2, invalidated):** To trigger loading page 2 of the School list, first tried `listbox.scrollTop = listbox.scrollHeight` plus a dispatched synthetic `'scroll'` `Event`. This did **not** trigger loading — rendered option count stayed at exactly 100 before and after, confirmed via direct instrumentation on the live page. The widget's scroll listener requires a real, trusted input event; synthetic DOM mutation and dispatched events are silently ignored.

**Related, in the same code area but from a prior session (`docs/plans/2026-08-06-002-fix-auto-apply-field-discovery-and-historical-failures-plan.md`):** an earlier hydration-race bug in the same file used `page.goto(job_url, wait_until="load")` as a first attempt to fix a related timing issue (a combobox permanently broken by interacting with it before a Remix/React SPA finished hydrating). That fix was rejected — `wait_until="load"` hung indefinitely on a real board with persistent connections — and replaced with a bounded, best-effort `_goto_and_settle()` helper instead of a hard wait condition. The same "don't trust an unbounded wait condition against a real third-party page" lesson applies directly to both bugs here.

## Solution

`apps/auto_apply/greenhouse_form/client.py`, `_extract_options()`, `COMBOBOX_SELECT` branch:

```python
if field_type == COMBOBOX_SELECT:
    try:
        control.click()
        listbox = page.get_by_role("listbox")
        # Wait for an actual *option* to render, not for the (possibly
        # still-empty) listbox container to become "visible" -- some
        # Greenhouse widgets populate options asynchronously after the
        # container mounts.
        listbox.first.get_by_role("option").first.wait_for(
            state="visible", timeout=_COMBOBOX_OPTION_TIMEOUT_MS  # 5_000
        )
        # A single page of options isn't necessarily the whole list --
        # some widgets are backed by a remote paginated API and only load
        # the next page in response to a REAL, trusted scroll (a
        # programmatic scrollTop assignment does not trigger it).
        # Bounded/best-effort: stops early once the count stops growing.
        options_locator = listbox.first.get_by_role("option")
        previous_count = options_locator.count()
        for _ in range(_COMBOBOX_SCROLL_ITERATIONS):  # 5
            options_locator.last.scroll_into_view_if_needed()
            page.wait_for_timeout(_COMBOBOX_SCROLL_WAIT_MS)  # 500
            current_count = options_locator.count()
            if current_count <= previous_count:
                break
            previous_count = current_count
        raw_options = listbox.first.get_by_role("option").all_text_contents()
    except Exception:
        raw_options = []
    finally:
        page.keyboard.press("Escape")
```

Two new regression fixtures reproduce each behavior deterministically without hitting real network endpoints:
- `apps/auto_apply/tests/fixtures/greenhouse_delayed_combobox_options_form.html` — `setTimeout`-delayed option rendering (async-populate race)
- `apps/auto_apply/tests/fixtures/greenhouse_paginated_combobox_options_form.html` — a fake 3-page infinite-scroll listbox using a native `scroll` event listener checking `scrollTop + clientHeight >= scrollHeight` (pagination-behind-real-scroll)

Corresponding tests in `apps/auto_apply/tests/test_greenhouse_form_client.py`: `test_inspect_waits_for_combobox_options_that_render_after_the_listbox_opens`, `test_inspect_scrolls_combobox_to_load_options_beyond_the_first_page`.

## Why This Works

- Waiting on `listbox.first.get_by_role("option").first` rather than the listbox container matches what actually varies across widgets. Some containers mount instantly but populate content later; waiting on container visibility alone tells you nothing about whether real option content exists yet, and — per the fixture evidence above — can even time out *before* content ever gets a chance to render.
- `scroll_into_view_if_needed()` on the last rendered option produces a real, trusted scroll interaction via Playwright's actionability machinery, which is what the paginated widget's native `scroll` listener requires. It's layout-based rather than viewport-pixel-based (unlike `mouse.wheel(0, N)` at fixed coordinates), so it's robust across headless environments and viewport sizes.
- The scroll loop is bounded (5 iterations) and self-terminating (stops once `current_count <= previous_count`), so small/non-paginated lists (Degree: 10 items, Country: 244 items) converge after one no-op iteration with no added latency, while large paginated lists (School) pull in meaningfully more real data within a fixed time budget.
- Verified live against the real job posting after each fix: before any fix, School=0, Degree=0. After the render-race fix, Degree=10 (deterministic), School=100 (still page-1-only, sometimes 0 depending on timing). After the pagination fix, School=400 (up from 100, out of 2,466 real total), Degree=10 (unaffected), Country=244 (unaffected, a bundled non-paginated list). Full project suite (`apps.auto_apply` + `apps.web`, 292 tests) green after both fixes.

## Prevention

- The pre-existing combobox fixture (`greenhouse_combobox_and_file_upload_form.html`) populates options synchronously with a small, complete, non-paginated set (e.g. `['Yes', 'No']`) — it structurally cannot exercise either failure mode. It models the general combobox shape, not the specific shape of Greenhouse's remote-data-backed "education" fields. When writing DOM-scraping/interaction code against a third-party widget, don't assume the widget's simplest observable state represents every state; explicitly test async-populated content and scroll/pagination-gated content as distinct scenarios.
- Keep both new fixtures as permanent regression coverage — they reproduce the race and pagination behaviors deterministically and fast (no real network calls), so they run on every change to `_extract_options()`.
- When validating scroll/interaction-triggered behavior in Playwright against widgets with native event listeners (`scroll`, `input`, etc.), prefer real-interaction APIs (`scroll_into_view_if_needed()`, `mouse.wheel()`, `click()`) over programmatic DOM/property mutation plus a dispatched synthetic event — many widgets specifically require trusted events and silently no-op on synthetic ones. Confirm this distinction directly against the live target before committing to an implementation; it isn't discoverable from reading the code alone.
- When a "wrong element matched" hypothesis seems plausible, verify with a full accessibility-tree/DOM snapshot taken at the moment of failure before writing a workaround — cheap, and can immediately falsify a plausible-sounding but wrong theory.
- This code path (`_extract_options()`'s `COMBOBOX_SELECT` branch) has now had two independent timing-shaped bugs found via live-browser verification against real Greenhouse boards, not fixture testing alone. Prior work in this same file (the Remix/React hydration race behind `_goto_and_settle()`) established the same pattern: an unbounded or naively-timed wait against a real third-party SPA is a recurring risk in this module, not a one-off. Any future change to discovery/fill timing in this file should be verified live, not just against fixtures, before being considered done.
- The discovered `options` snapshot for a `COMBOBOX_SELECT` field is inherently a best-effort, possibly-incomplete sample (even after this fix, School only discovers 400 of 2,466 real entries) — see "Related Issues" below for the code that depends on this and how it stays safe regardless.

## Related Issues

- `docs/plans/2026-08-09-002-fix-auto-apply-field-type-constrained-answers-plan.md` (scope decision, line ~37) reasoned that `COMBOBOX_SELECT` options are "often incomplete/unreliable (best-effort geocode-search results)" and left the field deliberately unconstrained/untouched for that reason — and shipped `answer_resolution._enforce_option_constraint()`, which rejects any answer not in the discovered `options` set. This finding shows the incompleteness for at least the School field had a concrete, partially-fixable root cause (render race + missing pagination scroll), not just inherent geocode fuzziness — but the plan's actual code decision (fail-closed, graceful submit-time handling, and relying on `_fill_combobox()`'s live-typed send-time search rather than the draft-time snapshot as the authoritative path) still holds and needs no change. This is additive context for anyone revisiting that scope decision.
- `docs/plans/2026-08-06-002-fix-auto-apply-field-discovery-and-historical-failures-plan.md` — prior related work in the same file: the Remix/React hydration-race fix (`_goto_and_settle()`), a reCAPTCHA-v3-invisible-badge false-positive found as a side effect of that fix, and an option-substring-matching-ambiguity fix (`"India"` vs `"British Indian Ocean Territory"`) in the same combobox-fill code path. Establishes the "verify live, not just against fixtures" discipline this fix also followed.
- `docs/residual-review-findings/fix-auto-apply-field-type-constrained-answers.md` (line ~121) flags "draft-time options snapshot staleness vs. the live form" as a residual/known-risk item from the option-constraint feature's own review. This fix addresses the pagination dimension of that staleness concretely; general snapshot staleness (the live form's options changing between draft and send) remains a separate, still-open concern.
