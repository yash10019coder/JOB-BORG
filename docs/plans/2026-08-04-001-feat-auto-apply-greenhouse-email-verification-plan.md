---
title: Auto-Apply Greenhouse Email Verification (Gmail Full Automation)
type: feat
status: active
date: 2026-08-04
---

# Auto-Apply Greenhouse Email Verification (Gmail Full Automation)

## Summary

Some Greenhouse boards show a second-factor screen after the application form is submitted: Greenhouse emails a 6-digit code to the candidate and requires it entered into a follow-up form before the application is truly accepted. Today `GreenhouseFormClient.submit()` has no concept of this — it just times out waiting for its known success signals and the draft is misclassified `SUBMISSION_FAILED` (confirmed live against a real Alpaca job posting). This plan adds full automation: a user connects their Gmail account once (OAuth, `gmail.readonly`), and when the interstitial is detected mid-submit, the same live Playwright session polls that user's Gmail for the fresh code, enters it, and completes the submission — all within one Celery task invocation, no manual step for the candidate.

---

## Problem Frame

Auto-apply's first slice (issue #44, `docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md`) didn't anticipate this interstitial. Research during planning surfaced two facts that shape the design:

- **Google's Gmail OAuth `gmail.readonly` scope is *restricted*, not merely sensitive.** Production access requires app verification plus an annual CASA security assessment — realistically 4-12 weeks, recurring cost, and real uncertainty whether "extract a job-application OTP" cleanly fits Google's approved-use-case rubric. **This is tracked as a parallel compliance workstream, not an implementation unit in this plan** — engineering proceeds now; verification submission runs alongside it. Until verified, the app is capped at 100 users in "Testing" status and refresh tokens expire after 7 days.
- **The verification page is very likely tied to more than cookies** — CSRF tokens or one-time nonces embedded in the rendered form are a documented risk for teardown/reconstruct-via-`storage_state` approaches. Confirmed as a design constraint: the *same* live Playwright page/context must stay open for the whole wait, not be recreated in a second task.

**Origin:** this plan has no upstream `ce-brainstorm` requirements document; requirements were established directly with the user (see Requirements below), informed by repo research (`apps/auto_apply/greenhouse_form/client.py`, `apps/auto_apply/models.py`, `apps/auto_apply/tasks.py`) and external research (Google OAuth verification process, Gmail API query semantics, `django-cryptography` maintenance status, Celery `countdown`/Canvas guidance, Playwright `storage_state` limitations).

---

## Requirements

- R1. `submit()` detects the Greenhouse verification interstitial as a distinct third outcome — not success, not the existing `GreenhouseFormSubmissionFailed` — via a new typed exception in the family of `GreenhouseFormChallenged`/`GreenhouseFormSchemaMismatch`.
- R2. A user can connect their Gmail account from their account/settings page via explicit OAuth consent (`gmail.readonly`). Never triggered implicitly by an application.
- R3. Refresh tokens are stored encrypted at rest, one per user.
- R4. When the interstitial is detected and the user has an active Gmail connection, the system polls that user's Gmail for the fresh verification email (narrowed by sender/time window), extracts the 6-digit code, enters it into the same live Playwright session, and submits.
- R5. The full submit → detect → poll → enter → confirm sequence runs inside one Celery task invocation with a bounded wait budget (~3-4 minutes); the browser session is never torn down and reconstructed mid-flow.
- R6. Every failure mode is an explicit, distinct, testable outcome with its own `ReasonCode` — no Gmail connection on file, code never arrived within budget, Gmail token revoked/expired, code rejected by Greenhouse, ambiguous/multiple matching emails — never a single generic catch-all, never a silent retry loop.
- R7. A revoked/expired Gmail token (`RefreshError`/`invalid_grant`) marks the stored connection inactive and surfaces to the user; the system never blind-retries a dead connection.
- R8. A user can disconnect Gmail from within the app; disconnecting calls Google's token revocation endpoint, not just a local delete (Google issues no revocation webhook, so this is the only way the app and Google agree access is actually gone).
- R9. The verification code and any Gmail message content are used transiently (in-memory, task-local) and never logged or persisted to the database or error tracking.

**Scope note:** no upstream brainstorm supplied A/F/AE-style IDs; requirements above are the traceability anchor for this plan.

---

## Scope Boundaries

- The Google OAuth restricted-scope verification / CASA submission process itself — tracked as a parallel compliance workstream outside this plan's implementation units.
- A manual "type the code yourself" fallback UI for users without a Gmail connection, or when auto-retrieval fails — today's normal `SUBMISSION_FAILED`-style visibility in the review queue is sufficient for v1 (the draft fails with a specific reason code the user can see); a dedicated manual-entry UI is a candidate follow-up, not required here.
- KMS envelope encryption for the refresh token (v1 uses a single application-level encryption key from env; envelope encryption is a hardening follow-up).
- Gmail push notifications (`watch()` + Pub/Sub) — polling is sufficient at this volume (one user, one code, a few minutes, once per verification-required submission).
- Multi-provider abstraction (IMAP, Outlook, etc.) — Gmail-only for now; a pluggable registry here would repeat the LLM-provider-registry over-abstraction already flagged as a P2 finding on the original auto-apply plan.
- Any change to `AutoApplyDraft.Status` — the whole wait happens inside the existing `SENDING` status; only `ReasonCode` grows.

### Deferred to Follow-Up Work

- Manual code-entry fallback UI.
- KMS-based envelope encryption for stored refresh tokens.
- Additional inbox providers behind a pluggable interface, if ever needed.

---

## Context & Research

### Relevant code and patterns to follow

- **`apps/auto_apply/captcha/base.py`** — the `CaptchaSolver` Protocol + registry, injected into `GreenhouseFormClient`, fails closed when unset. The verification-code retrieval step should be modeled the same way: a small Protocol (`get_code(user, since, timeout_seconds) -> str | None`) injected into `GreenhouseFormClient`, not threaded through `tasks.py`/multiple Celery calls. This keeps the browser session's lifetime entirely inside one synchronous `submit()` call, sidestepping the storage_state/CSRF risk entirely.
- **`apps/auto_apply/greenhouse_form/client.py`** — `_check_success_signal`, `_CONFIRMATION_TEXT_PATTERNS`, `GreenhouseFormChallenged`, `_challenge_detected` are the direct pattern for detecting and typing a new intermediate page state. `_goto_and_settle`'s bounded-wait pattern (`domcontentloaded` + best-effort `networkidle` with a timeout) is the template for "wait for something with a budget, never hang indefinitely."
- **`apps/auto_apply/models.py`** — `AutoApplyDraft.ReasonCode` (`SCHEMA_MISMATCH, FORM_LOAD_FAILED, UNANSWERABLE_REQUIRED, CAPTCHA_CHALLENGED, SUBMISSION_FAILED, SENDING_TIMEOUT, UNEXPECTED_ERROR`) is where the new reason codes are added, following the existing `TextChoices` convention.
- **`apps/auto_apply/tasks.py`** — `_SUBMIT_SOFT_TIME_LIMIT_SECONDS`/`_SUBMIT_TIME_LIMIT_SECONDS` currently freeze at module-import time (a known, previously-flagged P1 bug: `@override_settings` doesn't affect them). Since this plan adds a verification-wait budget on top of the existing submit budget — touching this exact formula — the import-time freezing bug should be fixed as part of this change, not inherited.
- **`apps/accounts/models.py`** — `Profile`'s `OneToOneField(settings.AUTH_USER_MODEL)` is the shape for a new `GmailConnection` model (account-level, not job/draft-level).
- **`apps/matching/tasks.py:34-36`** — the one existing `apply_async(countdown=...)` example in the codebase; not used directly here (R5 keeps everything in one task) but confirms Celery delayed-task conventions if a future iteration needs them.
- **`apps/web/views.py`** — `@login_required` + `@require_POST` function-view convention for state-changing actions (`send_auto_apply_draft`, `edit_auto_apply_draft`) is the template for the new Gmail connect/callback/disconnect views.

### External research findings (informing Key Technical Decisions below)

- `gmail.readonly` is required (not `gmail.metadata`) since the code is in the email body, not headers.
- Gmail's `newer_than:` operator only supports day/month/year granularity, not minutes — narrow with `after:<unix_ts>` (day-granular) plus an application-side filter on the message's actual timestamp for true few-minute precision.
- `django-cryptography-5` (PyPI) is the maintained fork supporting Django 5.1/Python 3.12 — the original `django-cryptography` and `django-fernet-fields` are unmaintained for this stack.
- `google-auth-oauthlib`'s web-server `Flow` (not `InstalledAppFlow`) is the correct flow; `access_type='offline'` + `prompt='consent'` needed to reliably get a refresh token; revocation surfaces later as `google.auth.exceptions.RefreshError` on the next refresh attempt, not a push notification.
- Realistic transactional-email latency is 5-60 seconds typically, with occasional 1-2 minute outliers (first-time sender/recipient pairs get extra Gmail-side scanning) — sizes the ~3-4 minute poll budget.

---

## Key Technical Decisions

1. **Verification-code retrieval is a synchronous, injected collaborator inside `GreenhouseFormClient.submit()`, not a second Celery task.** Mirrors the `CaptchaSolver` pattern exactly. Avoids the CSRF/nonce risk of reconstructing a Playwright context via `storage_state` in a later task, and avoids Celery `countdown`/idempotency complexity entirely (rejected per research: `apply_async(countdown=...)` is fine for pure delays but pairs poorly with "resume the same in-memory browser object," which Celery gives no guarantee of across invocations).
2. **One Celery task, bounded total duration.** The existing `_SUBMIT_SOFT_TIME_LIMIT_SECONDS`/`_SUBMIT_TIME_LIMIT_SECONDS` formula is fixed (module-import-time freezing bug) and extended with a separate, explicit verification-poll budget (new `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS` setting, default 180s) added on top of the base submit budget — not folded silently into the existing constant, so the two costs stay independently visible and tunable.
3. **Encryption: application-level Fernet field now, KMS deferred.** `django-cryptography-5`'s encrypted field wraps `GmailConnection.refresh_token`; the Fernet key itself comes from a new `GMAIL_TOKEN_ENCRYPTION_KEY` env setting (not `SECRET_KEY`, so it can be rotated independently). Documented as a v1 tradeoff — envelope encryption via KMS is deferred.
4. **No new `AutoApplyDraft.Status` value.** The whole detect → poll → enter → confirm sequence completes (or fails) inside the same `SENDING` task invocation; only `ReasonCode` grows with new fail-closed outcomes (R6). This avoids touching `ACTIVE_STATUSES` and the uniqueness constraint at all.
5. **Fail-closed, per-outcome reason codes, not a generic `VERIFICATION_FAILED`.** Per the location-matching lesson already documented in `docs/solutions/` ("unresolved is a first-class state, not a collapsed fallback"): distinct codes for no connection, timeout, revoked token, rejected code, and ambiguous match.

---

## System-Wide Impact

- **`apps/auto_apply`**: new Gmail-provider module, client.py detection/fill changes, tasks.py wiring and time-limit fix, new `ReasonCode` values.
- **`apps/accounts`**: new `GmailConnection` model + migration, new connect/callback/disconnect views and URLs.
- **`config/settings/base.py`**: new env-backed settings (`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GMAIL_TOKEN_ENCRYPTION_KEY`, `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS`).
- **`requirements/base.txt`**: `google-auth-oauthlib`, `google-api-python-client`, `google-auth`, `django-cryptography-5`, `cryptography` (if not already present transitively).
- **Ops/compliance** (outside this plan): Google OAuth verification + CASA submission should start in parallel with engineering, given its multi-week critical path.

---

## Implementation Units

### U1. Gmail OAuth connection model and encrypted token storage

**Goal:** A per-user `GmailConnection` model storing OAuth credentials needed to re-derive a working Gmail API client later, with the refresh token encrypted at rest.

**Requirements:** R2, R3

**Dependencies:** None

**Files:**
- Modify: `apps/accounts/models.py` (add `GmailConnection`)
- Create: `apps/accounts/migrations/00XX_gmailconnection.py`
- Modify: `config/settings/base.py` (add `GMAIL_TOKEN_ENCRYPTION_KEY`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`)
- Modify: `requirements/base.txt` (`django-cryptography-5`, `cryptography`, `google-auth-oauthlib`, `google-api-python-client`, `google-auth`)
- Test: `apps/accounts/tests/test_gmail_connection.py`

**Approach:** `GmailConnection` is `OneToOneField(settings.AUTH_USER_MODEL, related_name="gmail_connection")`, following `Profile`'s existing shape. Fields: encrypted `refresh_token` (django-cryptography-5's encrypted text field), `scopes` (plain text, not secret), `is_active` (boolean, flipped false on `RefreshError`), `connected_at`/`updated_at`. No client-side lookup/filter needed on the encrypted column (per research: Fernet ciphertext isn't stable across encryptions, so it must never be a lookup key) — always accessed via the user FK.

**Patterns to follow:** `apps/accounts/models.py`'s `Profile` model shape; `created_at`/`updated_at` pair convention from AGENTS.md.

**Test scenarios:**
- Happy path: creating a `GmailConnection` for a user, saving and reloading it, decrypts back to the original refresh token.
- Edge case: two different users' connections don't collide; deleting the user cascades the connection.
- Test expectation: no behavioral logic beyond model/migration correctness at this unit — encryption round-trip is the key assertion.

**Verification:** A `GmailConnection` row can be created, saved, and reloaded with the refresh token decrypting correctly; the raw DB column value is not the plaintext token (spot-check via raw SQL in the test).

---

### U2. Gmail connect / callback / disconnect views

**Goal:** Let a user initiate Gmail OAuth consent, complete the callback, and disconnect later (with real Google-side revocation).

**Requirements:** R2, R8

**Dependencies:** U1

**Files:**
- Modify: `apps/web/views.py` (add `connect_gmail`, `gmail_oauth_callback`, `disconnect_gmail`)
- Modify: `apps/web/urls.py`
- Modify: `templates/web/profile.html` (or wherever account settings live) — add a Gmail connect/disconnect control
- Test: `apps/web/tests/test_gmail_oauth_views.py`

**Approach:** `connect_gmail` (`@login_required`) builds a `google_auth_oauthlib.flow.Flow` with `access_type='offline'`, `prompt='consent'`, and the `gmail.readonly` scope, redirects to Google's consent screen. `gmail_oauth_callback` exchanges the code, and **only overwrites the stored refresh token if Google actually returned a new one** (`credentials.refresh_token is not None`) — a re-consent that doesn't return one keeps the existing stored token, per Google's documented behavior. `disconnect_gmail` (`@require_POST`) calls Google's revoke endpoint (`https://oauth2.googleapis.com/revoke`) with the stored token, then deletes/deactivates the local `GmailConnection` regardless of whether the revoke call succeeds (best-effort revoke, but local disconnect always takes effect).

**Patterns to follow:** `apps/web/views.py`'s `@login_required`/`@require_POST` function-view style (e.g. `send_auto_apply_draft`).

**Test scenarios:**
- Happy path: full connect → callback flow creates a `GmailConnection` with the returned refresh token.
- Edge case: re-consent that returns no new refresh token doesn't clobber the existing stored token.
- Error path: callback with a denied/invalid OAuth response is handled without a 500 and without creating a partial `GmailConnection`.
- Integration: disconnect calls the real revoke endpoint (mocked in tests) and removes local access even if the revoke call itself fails/errors.

**Verification:** Manual OAuth round-trip against a real Google test app succeeds; automated tests cover the callback edge cases above without hitting the network.

---

### U3. Detect the Greenhouse verification interstitial

**Goal:** `GreenhouseFormClient.submit()` recognizes the post-submit verification-code page as a distinct, typed outcome.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/client.py`
- Modify: `apps/auto_apply/greenhouse_form/exceptions.py` (new `GreenhouseFormVerificationRequired`)
- Test: `apps/auto_apply/tests/test_greenhouse_form_client.py`
- Test fixture: `apps/auto_apply/tests/fixtures/greenhouse_email_verification_form.html` (new)

**Approach:** After `_click_submit`, before/alongside the existing `_check_success_signal` poll, check for the verification page's signature (a code-input field plus recognizable copy, e.g. "Enter the code we sent to..."). Real Greenhouse markup for this page hasn't been captured yet in this codebase (the original bug report was a screenshot, not a saved fixture) — **the exact selector/text pattern is an implementation-time unknown**, to be confirmed against a live board before finalizing (mirrors the `_CONFIRMATION_TEXT_PATTERNS` precedent, which needed a live-Greenhouse correction after initial assumptions were wrong). If detected, raise `GreenhouseFormVerificationRequired` carrying the field locator needed to enter the code later, rather than proceeding to `_check_success_signal`'s timeout path.

**Execution note:** Confirm the live page structure via a read-only Playwright inspection against a real Greenhouse board showing this interstitial before writing the detection selector — do not guess from the screenshot alone.

**Patterns to follow:** `_challenge_detected`'s selector-based detection; `GreenhouseFormChallenged`'s exception shape.

**Test scenarios:**
- Happy path: fixture with a verification-code form renders after submit; `submit()` raises `GreenhouseFormVerificationRequired` rather than treating it as success or `GreenhouseFormSubmissionFailed`.
- Edge case: a normal success confirmation is not misdetected as the verification page (regression guard against false positives, mirroring the reCAPTCHA v3 false-positive bug fixed in `3f76152`).
- Edge case: a genuine failure page (validation errors) is still classified `GreenhouseFormSubmissionFailed`, not `GreenhouseFormVerificationRequired`.

**Verification:** New unit tests pass; live read-only verification against a real Greenhouse board confirms the detection selector matches the actual rendered page.

---

### U4. Gmail verification-code provider

**Goal:** A small, injectable collaborator that, given a user, searches that user's Gmail for a fresh Greenhouse verification code and returns it (or `None` on timeout/failure), following the `CaptchaSolver` Protocol pattern.

**Requirements:** R4, R6, R7, R9

**Dependencies:** U1

**Files:**
- Create: `apps/auto_apply/gmail/__init__.py`, `apps/auto_apply/gmail/client.py` (Protocol + concrete `GmailVerificationCodeProvider`)
- Test: `apps/auto_apply/tests/test_gmail_verification_code_provider.py`

**Approach:** `get_code(user, since: datetime, timeout_seconds: int) -> str | None`. Internally: reconstructs `google.oauth2.credentials.Credentials` from the user's `GmailConnection` (decrypting the refresh token), polls `messages.list` (`q=from:<greenhouse sender pattern> after:<epoch>`, narrowed further by matching the message's actual internal timestamp against `since` for true few-minute precision, per research), then `messages.get` + a regex extraction of a 6-digit code from the body. Polls every few seconds with light backoff, up to `timeout_seconds` (default from `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS`). On `google.auth.exceptions.RefreshError` (revoked/expired token), marks the `GmailConnection.is_active = False` and returns `None` — never retries a known-dead connection. No email body or extracted code is logged; only structured outcome (found/not-found/error-type) is recordable.

**Patterns to follow:** `apps/auto_apply/captcha/base.py`'s Protocol shape; `apps/auto_apply/llm/base.py`'s provider-resolution style (kept single-vendor here per Scope Boundaries — no registry).

**Test scenarios:**
- Happy path: a mocked Gmail API response containing a valid 6-digit code within the time window is extracted correctly.
- Edge case: multiple matching messages in the window — the most recent one wins (or ambiguity is treated as a first-class failure per R6, whichever the implementer confirms is safer — recommend: pick most-recent-by-timestamp, but this is an implementation-time judgment call).
- Edge case: no matching message within `timeout_seconds` returns `None`, not an exception.
- Error path: `RefreshError` on token refresh — connection is marked inactive, `get_code` returns `None`, no retry attempted.
- Error path: Gmail API transient error (e.g. 5xx) is retried a bounded number of times within the timeout budget, not treated as an immediate hard failure.
- Integration: a code embedded in a realistic Greenhouse-style email body is correctly extracted via the regex (not a false match on an unrelated 6-digit number elsewhere in the email, e.g. a phone number fragment).

**Verification:** Unit tests pass with mocked Gmail API responses; no real network calls in the test suite.

---

### U5. Wire verification handling into `submit()` and reason codes

**Goal:** When `GreenhouseFormVerificationRequired` is raised (U3) and a code provider is available (U4), `submit()` calls it synchronously, enters the code into the live page, and completes the submission — or fails closed with a specific reason.

**Requirements:** R1, R4, R5, R6

**Dependencies:** U3, U4

**Files:**
- Modify: `apps/auto_apply/greenhouse_form/client.py` (`GreenhouseFormClient.__init__` gains an optional `verification_code_provider`; `submit()` handles the new branch)
- Modify: `apps/auto_apply/models.py` (`AutoApplyDraft.ReasonCode` gains `NO_GMAIL_CONNECTION`, `VERIFICATION_CODE_TIMEOUT`, `VERIFICATION_TOKEN_REVOKED`, `VERIFICATION_CODE_REJECTED`)
- Modify: `apps/auto_apply/tasks.py` (construct `GreenhouseFormClient` with a `GmailVerificationCodeProvider(user)` when the user has an active `GmailConnection`, else `None`; map new exceptions to the new reason codes; fix the module-import-time time-limit freezing bug while extending the formula with the verification-poll budget)
- Test: `apps/auto_apply/tests/test_greenhouse_form_client.py`, `apps/auto_apply/tests/test_tasks.py`

**Approach:** Mirrors the existing CAPTCHA fail-closed pattern exactly: if `verification_code_provider` is `None` when the interstitial is hit, fail immediately with `NO_GMAIL_CONNECTION` (no point polling). If present, call `provider.get_code(user, since=<time of submit>, timeout_seconds=...)`; `None` back means timeout → `VERIFICATION_CODE_TIMEOUT` (or `VERIFICATION_TOKEN_REVOKED` if the provider signals the connection went inactive during this call — implementer's choice on exact signal shape between U4 and U5, an implementation-time detail). A retrieved code that Greenhouse itself rejects (still on the verification page after entering it) is `VERIFICATION_CODE_REJECTED`. Time-limit fix: compute `_SUBMIT_SOFT_TIME_LIMIT_SECONDS`/`_SUBMIT_TIME_LIMIT_SECONDS` lazily (function or `@shared_task`-time evaluation) rather than at import time, and add `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS` to the budget so the sweep-vs-task-kill ordering invariant (Celery kills the task before the staleness sweep reclaims it) still holds with the longer possible duration.

**Execution note:** Write the fail-closed "no connection" and "timeout" paths test-first — these are the safety-critical branches (never silently mark a draft Applied/successful without a real Greenhouse confirmation).

**Patterns to follow:** `apps/auto_apply/greenhouse_form/client.py`'s existing CAPTCHA-challenge handling and its mapping to `ReasonCode` in `tasks.py`'s except-block.

**Test scenarios:**
- Happy path: verification required, provider returns a valid code, code entered, submission succeeds — draft ends up `APPLIED`.
- Edge case: verification required, user has no `GmailConnection` — draft `FAILED` with `NO_GMAIL_CONNECTION`, no Gmail call attempted.
- Edge case: verification required, provider times out — draft `FAILED` with `VERIFICATION_CODE_TIMEOUT`.
- Edge case: provider signals the connection is now inactive (revoked mid-flow) — draft `FAILED` with `VERIFICATION_TOKEN_REVOKED`.
- Edge case: code entered but Greenhouse rejects it — draft `FAILED` with `VERIFICATION_CODE_REJECTED`, not misclassified as timeout.
- Integration: the full `submit_auto_apply_draft` task path (not just `GreenhouseFormClient` in isolation) exercises the new branches end-to-end with a fixture form, confirming `tasks.py`'s exception→reason-code mapping is wired correctly.
- Regression: existing CAPTCHA and schema-mismatch fail-closed paths are unaffected by the new branch (no accidental fallthrough).
- Time-limit fix: a test confirming `_SUBMIT_SOFT_TIME_LIMIT_SECONDS`-equivalent value now responds to `@override_settings` (regression test for the previously-flagged bug).

**Verification:** Full `apps/auto_apply` test suite passes; a live (read-only up to the point of actually submitting) run against a real Greenhouse board known to show this interstitial confirms the happy path end-to-end, following this codebase's established live-verification discipline for this feature.

---

### U6. Settings, requirements, and operational wiring

**Goal:** All new configuration is explicit, documented, and follows existing `env(...)`-backed settings conventions.

**Requirements:** Supports R2, R3, R5

**Dependencies:** U1, U4, U5

**Files:**
- Modify: `config/settings/base.py`
- Modify: `requirements/base.txt`
- Modify: `AGENTS.md` if it documents environment variables (confirm during implementation)

**Approach:** New settings: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (from Google Cloud Console, no default), `GMAIL_TOKEN_ENCRYPTION_KEY` (Fernet key, no default — must fail loudly if unset in production rather than silently using a weak default), `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS` (default 180, near `AUTO_APPLY_SENDING_TIMEOUT_SECONDS` in `base.py` per existing convention of grouping domain-tunable constants).

**Test expectation:** none — pure configuration; covered indirectly by U1/U5's tests exercising the settings values.

**Verification:** App fails to start (or fails loudly at first use) if `GMAIL_TOKEN_ENCRYPTION_KEY` is unset in an environment where Gmail features are reached, rather than silently storing tokens insecurely.

---

## Risks & Dependencies

- **Google OAuth verification/CASA timeline (4-12 weeks) and approval uncertainty** — tracked as a parallel workstream; engineering ships against Google's "Testing" publish status in the meantime (100-user cap, 7-day refresh-token expiry), a known, accepted limitation until verification completes.
- **Unconfirmed live page structure for the verification interstitial** — U3's detection selector is an implementation-time unknown until validated against a real Greenhouse board; budget time for this before finalizing.
- **Gmail delivery latency variance** — the ~3-4 minute poll budget is an estimate from general transactional-email research, not Greenhouse-specific; validate against real test emails and adjust `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS` if needed.
- **No existing consent/privacy-disclosure mechanism in this codebase** (already flagged, unresolved, on the original auto-apply plan) — granting Gmail inbox read access is a materially larger version of that same gap; the user-facing consent screen (U2) needs to be explicit about exactly what's read and why, independent of Google's own consent screen.

---

## Test Strategy

Unit tests for each new component in isolation (encrypted model field, OAuth views with mocked Google endpoints, Gmail provider with mocked API responses, client.py detection/fill with fixture HTML), plus one integration test exercising the full `submit_auto_apply_draft` task path end-to-end against a fixture. Live, read-only verification against a real Greenhouse board for the detection selector (U3) and the full happy path (U5), following this codebase's established practice of validating Playwright-facing assumptions live before shipping (per `_CONFIRMATION_TEXT_PATTERNS` and the hydration-race/combobox fixes in commit `3f76152`).
