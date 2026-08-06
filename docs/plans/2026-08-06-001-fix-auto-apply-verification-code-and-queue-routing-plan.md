---
title: Fix Auto-Apply Verification Code Format and Submit Queue Routing
type: fix
status: completed
date: 2026-08-06
---

# Fix Auto-Apply Verification Code Format and Submit Queue Routing

## Summary

Two confirmed, live-evidenced bugs found while validating the Greenhouse email-verification feature (PR #48) end-to-end against a real Alpaca posting:

1. The verification-code extraction regex was hard-coded to match exactly 6 numeric digits, but the real Greenhouse interstitial asks for an 8-character code (confirmed via a captured debug screenshot and its accompanying accessibility-tree snapshot). This has already been fixed and tested in the working tree but not yet committed.
2. `submit_auto_apply_draft` is routed to a dedicated Celery queue (`auto_apply_submit`) via `CELERY_TASK_ROUTES`, but the `worker` service in `docker-compose.yml` only consumes the default queue — confirmed live via `redis-cli llen auto_apply_submit` showing unconsumed messages. Every real submission attempt silently never runs.

---

## Requirements

- R1. The verification-code extraction logic correctly matches the real 8-character code format used by Greenhouse's interstitial, without reintroducing false-positive matches on plain words adjacent to the trigger phrasing.
- R2. `submit_auto_apply_draft` tasks are actually consumed and executed by a running worker, not silently queued forever.

## Scope Boundaries

- Not in scope: determining the exact charset Greenhouse uses for the 8-character code (digits-only vs. alphanumeric) — no real verification email has been captured yet; the current fix uses a documented, conservative alphanumeric-with-at-least-one-digit assumption pending that capture.
- Not in scope: splitting `submit_auto_apply_draft` into a fully separate dedicated worker deployment (smaller pool, isolated scaling) — the plan's original intent for a "small dedicated worker pool" is deferred; this fix only makes the existing worker consume the queue it's supposed to.
- Not in scope: the host disk-space and container restart-policy issues flagged during this session's debugging — operational concerns unrelated to this code fix.

---

## Implementation Units

### U1. Verification-code extraction: match the real 8-character code format

**Goal:** Extraction correctly recognizes Greenhouse's actual 8-character verification code instead of only 6-digit numeric codes.

**Requirements:** R1

**Dependencies:** none

**Files:**
- Modify: `apps/auto_apply/email_verification/extraction.py`
- Modify: `apps/auto_apply/tests/test_email_code_extraction.py`

**Approach:** Widen `CONTEXTUAL_CODE_REGEX` and the bare-code fallback (renamed `BARE_CODE_REGEX`) from `\d{6}` to a 6-10 character alphanumeric token requiring at least one digit — wide enough to cover the confirmed "8-character code" case, while the digit requirement guards against matching plain English words that sit directly next to the trigger phrasing in the real copy (e.g. "confirm", "human"). The gap between the trigger phrase and the code token must exclude only digits, not all alphanumerics, so intervening words like "is" or "code:" still allow the match. This work is already implemented and test-passing in the working tree; this unit is the commit/verification step, not fresh implementation.

**Patterns to follow:** Existing test structure in `test_email_code_extraction.py` (`SimpleTestCase`, one behavior per test method).

**Test scenarios:**
- Happy path: an 8-character alphanumeric code adjacent to verification phrasing is extracted correctly.
- Regression: existing 6-digit-code scenarios (realistic extraction, contextual-regex prioritization) still pass.
- False-positive guard: plain words with no digit (e.g. "confirm", "human") sitting directly next to the trigger phrase are never mistaken for a code when no real code is present in the text.
- No verification phrasing present: returns `None` regardless of digit-bearing tokens nearby.

**Verification:** `apps.auto_apply.tests.test_email_code_extraction` passes in full; full `apps.auto_apply` suite has no regressions.

---

### U2. Route the submit worker to actually consume its dedicated queue

**Goal:** `submit_auto_apply_draft` tasks are picked up and executed, not silently stuck in an unconsumed queue.

**Requirements:** R2

**Dependencies:** none

**Files:**
- Modify: `docker-compose.yml`

**Approach:** The `worker` service's Celery command has no `-Q` flag, so it defaults to consuming only `CELERY_TASK_DEFAULT_QUEUE` ("default"). Add the `auto_apply_submit` queue (added by this feature's `CELERY_TASK_ROUTES`) to the worker's consumed queue list so both queues are served by the same worker process. This is the minimal fix restoring correct behavior; a fully separate dedicated worker/pool is deferred (see Scope Boundaries).

**Patterns to follow:** N/A — single-line change to an existing `command:` value in `docker-compose.yml`.

**Test scenarios:**
- Test expectation: none -- this is infrastructure/deployment configuration with no Python-testable surface. Verification is operational (see below).

**Verification:** After restarting the `worker` service, its startup log's `[queues]` banner lists both `default` and `auto_apply_submit`. A `submit_auto_apply_draft` task enqueued via `.delay()` is picked up (`celery -A config inspect active` shows it running, not stuck) and the corresponding `AutoApplyDraft` reaches a terminal status (`applied` or `failed` with a reason code) rather than remaining in `sending` indefinitely.

---

## Risks & Open Questions

- The exact charset Greenhouse uses for its 8-character code is still unconfirmed (U1's fix is a documented judgment call, not a verified fact) — worth revisiting once a real verification email is captured.
- U2's fix keeps submission work on the same worker process as everything else (no isolation from other queues); if Playwright submissions under load start starving other tasks, revisit the "dedicated worker pool" idea deferred here.
