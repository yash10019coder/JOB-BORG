---
title: Auto-Apply Greenhouse Email Verification (IMAP App-Password Automation)
type: feat
status: active
date: 2026-08-04
---

# Auto-Apply Greenhouse Email Verification (IMAP App-Password Automation)

> **Supersedes an earlier OAuth-based draft of this plan.** The user rejected Gmail OAuth: `gmail.readonly` is a *restricted* scope requiring app verification plus an annual CASA security audit (realistically 4-12 weeks, recurring cost, uncertain approval for this use case). This plan uses IMAP + a stored app password instead, accepting that an app password grants broader access than OAuth would have (see Security Assessment).

## Context

Some Greenhouse job boards show a second-factor screen after the application form is submitted: Greenhouse emails a 6-digit code to the candidate, and it must be entered into a follow-up form before the application is truly accepted. Today `GreenhouseFormClient.submit()` (`apps/auto_apply/greenhouse_form/client.py`) has no concept of this — it exhausts its success-confirmation poll and the draft is misclassified `SUBMISSION_FAILED`. Confirmed live against a real Alpaca Greenhouse posting.

This plan was designed with a repo-research pass (exact file:line references for every relevant existing pattern), institutional-learnings review, and external research (Google's 2026 app-password policy verified via live search: app passwords still work for IMAP on consumer Gmail accounts with 2-Step Verification on; Workspace admins can disable them; they're silently revoked on a Google password change).

Both open decisions below were reviewed and resolved by the user after seeing the full plan.

---

## Decisions Resolved

1. **Infra scope: proceed with the full infra change.** Raise `AUTO_APPLY_SENDING_TIMEOUT_SECONDS` 300s→600s, add a dedicated Celery queue for `submit_auto_apply_draft`, add the per-user Redis lock (D1, D7). U7 implements all three.
2. **UX framing: lead with the dedicated-inbox recommendation.** U4's credential-connect page copy frames a throwaway/dedicated inbox as the safer default; using the user's main inbox is presented as the less-safe convenience option.

---

## Summary

Add full automation for Greenhouse's post-submission email verification: a user stores an IMAP app password once (address + password + host/port); when the verification interstitial is detected mid-submit, the *same live Playwright page* polls that user's inbox for the fresh code, types it, and completes the submission — all inside one Celery task invocation, with every failure mode a distinct, fail-closed reason code. No manual step for the candidate.

---

## Corrections Made to the Original (OAuth-era) Framing

These changed the design, not just the wording:

1. **The injected provider must be DB-free at the `GreenhouseFormClient` boundary.** `GreenhouseFormClient` is deliberately DB-free by design. The credential binds at construction in `tasks.py` (`ImapEmailCodeProvider(credential)`), not by passing a `User` into the client.
2. **"Most recent email wins" is unsafe.** Two verification emails can legitimately arrive seconds apart for different employers with concurrent auto-applies in flight. Ambiguity (≥2 candidate emails with *differing* codes) is a fail-closed outcome, plus a per-user Redis lock serializes verification waits.
3. **The existing debug-artifact feature (`AUTO_APPLY_DEBUG_ARTIFACT_DIR`) violates "never persist the code."** It takes full-page screenshots on failure; any failure after the code is typed would capture the live OTP to disk. Needs an explicit carve-out.
4. **A bare 6-digit regex over an unfiltered inbox is a credential-exfiltration primitive.** Matching requires sender allowlist **AND** contextual phrasing **AND** arrival strictly after the submit click — never "any 6 digits in a recent email."
5. **IMAP avoids Google's compliance process, not the underlying security question.** A Gmail app password grants full IMAP **and SMTP** access (read, delete, send-as) — materially broader than `gmail.readonly` would have been. Stated plainly, not glossed over (see Security Assessment).
6. **Raising the sweep timeout isn't free.** It delays stuck-draft recovery for every draft, not just verification ones. Accepted.

---

## Requirements

- **R1.** `submit()` detects the verification interstitial as a distinct typed outcome (new `GreenhouseFormError` subclass) — not success, not the existing `GreenhouseFormSubmissionFailed`.
- **R2.** User can save inbox credentials (address + IMAP app password + host/port) from their profile page.
- **R3.** The password is encrypted at rest.
- **R4.** On interstitial detection with credentials on file, poll the inbox for the fresh verification email, extract the 6-digit code, enter it in the *same* live Playwright session, and submit.
- **R5.** The whole sequence runs in ONE Celery task invocation, bounded; the browser session is never torn down and reconstructed mid-flow.
- **R6.** Distinct, testable, fail-closed failure states, each with its own `ReasonCode`: no credentials, code never arrived in budget, IMAP auth failed, inbox unreachable (transient), code rejected by Greenhouse, ambiguous/multiple matching emails.
- **R7.** IMAP auth failure marks the stored credential inactive and surfaces to the user; no blind retry. A transient/unreachable failure does *not* deactivate the credential (see D6).
- **R8.** User can remove/replace their stored credentials.
- **R9.** The verification code and any email content are used transiently only — never logged, never persisted to the DB, never in `error_message`, and never captured in debug screenshots once a code has been typed.

## Scope Boundaries

- **In:** Gmail-shaped consumer IMAP (`imap.gmail.com:993`) plus a small host allowlist (room for Outlook/Yahoo, unvalidated). One credential per user.
- **Out:** OAuth of any kind; SMTP; IMAP `IDLE` push; multiple credentials per user; a manual "type the code yourself" UI; Workspace/tenant-specific auth workarounds; KMS envelope encryption.
- **Explicitly not added:** any new `AutoApplyDraft.Status`. The whole detect → poll → enter → confirm sequence lives inside the existing `SENDING` status; only `ReasonCode` grows. `ACTIVE_STATUSES` and the hardcoded `Q(status__in=["drafted","sending"])` uniqueness constraint (`models.py`) stay untouched.

### Deferred to Follow-Up Work

- A manual "type the code yourself" fallback UI.
- KMS envelope encryption for stored app passwords.
- OAuth (once/if a CASA audit becomes viable) — the Protocol seam makes this a one-module swap.
- Periodic background credential health-checks (rejected for v1 — would require a second consumer of the credential, which the Security Assessment's mitigations explicitly forbid).

---

## Key Technical Decisions

### D1. The Celery time budget — move authority from the decorator into the task body

**The constraint.** `apps/auto_apply/tasks.py` currently derives task time limits from the sweep threshold by subtraction, **at module import time**: `_SUBMIT_SOFT_TIME_LIMIT_SECONDS = max(30, settings.AUTO_APPLY_SENDING_TIMEOUT_SECONDS - 60)` (→240s with the 300s default), `_SUBMIT_TIME_LIMIT_SECONDS = _SUBMIT_SOFT_TIME_LIMIT_SECONDS + 30` (→270s). This is a known bug (breaks `@override_settings` in tests, already flagged in a prior review) — and it's directly in the way here, since an inline IMAP wait now competes with browser automation for the same budget. Browser work alone (page settle + schema discovery + combobox fills + submit + confirm) plausibly consumes ~65-100s worst case, leaving too little of the 240s for an email round-trip whose realistic tail is 1-2 minutes.

**Rejected alternatives:** shortening the poll to fit (guarantees intermittent failures on slow mail); splitting into two Celery tasks with `countdown` (violates R5 and the CSRF/nonce concern — Playwright objects aren't serializable and Celery gives no same-process guarantee); making the decorator's `time_limit=`/`soft_time_limit=` dynamic (Celery evaluates these at import time — there's no callable form, and `CELERY_TASK_ALWAYS_EAGER` test mode ignores time limits entirely, part of why the existing bug was never caught).

**Decision — three changes together:**

- **(a)** Compute the real budget at call time, inside the task body: `budget = _submit_budget_seconds()` (reads settings per-invocation) → `deadline = time.monotonic() + budget`. This is the actual fix for the import-time-freezing bug.
- **(b)** Thread the deadline into `GreenhouseFormClient.submit(..., deadline_monotonic=...)`. The verification poll gets `min(AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS, deadline - now)`; if the remaining slice is below a floor (~20s), fail closed immediately with the timeout outcome.
- **(c)** Keep a Celery hard `time_limit` as a pure backstop, a plain literal constant (e.g. 900s) — not derived from any setting. Drop `soft_time_limit` entirely.
- **(d)** Raise `AUTO_APPLY_SENDING_TIMEOUT_SECONDS` default 300→600. Operative budget becomes 540s (~100s browser + up to 180s poll + slack), sweep still strictly behind at 600s, hard kill behind that at 900s. Add a Django system check asserting the ordering invariant holds.

**Operational consequence:** a submit task can now occupy a worker slot for up to ~9 minutes holding a Chromium process. Route `submit_auto_apply_draft` to its own dedicated Celery queue with a small worker pool (`worker_prefetch_multiplier=1`).

### D2. IMAP library: stdlib `imaplib` + `email` — no new dependency

The required surface is small (`IMAP4_SSL` → `login` → `select(readonly=True)` → poll loop of `noop()`+`search()`+`fetch(BODY.PEEK[])`), stdlib gives TLS cert verification and socket timeouts for free, and every new dependency in the credential-handling path widens the supply-chain blast radius on the most sensitive secret this product will hold.

**Critical semantic:** a selected mailbox doesn't report newly-arrived mail until a command forces a sync — `NOOP()` must run before each `SEARCH()`, or the poll will never see mail arriving after `SELECT`. Single most likely implementation bug in this unit.

**Always `BODY.PEEK[]`, never `BODY[]`** (the latter marks mail read), plus `select(readonly=True)` as belt-and-braces. One connection held open for the whole poll (Gmail throttles rapid reconnects and caps concurrent connections).

### D3. Encryption: a small explicit Fernet module, not a third-party encrypted-field library

**Decision:** `apps/accounts/crypto.py` (~40 lines) wrapping `cryptography.fernet.MultiFernet`, with explicit `encrypt_secret()`/`decrypt_secret()` functions and a plain `TextField` holding base64 ciphertext — **not** a transparent encrypted model field.

Why explicit over transparent: a transparent field means `credential.app_password` yields plaintext from *any* code path (repr, admin, error-tracker variable capture, a stray debug log). Explicit decryption turns "did we leak it?" into a greppable question. The maintained third-party options for Django-5 encrypted fields are single-maintainer forks sitting on the highest-value secret this product holds; `cryptography` itself is ubiquitous and well-maintained, and the wrapper is genuinely small.

**Key management:** new setting `CREDENTIAL_ENCRYPTION_KEYS` (list of Fernet keys, first encrypts / all decrypt via `MultiFernet` — free key rotation). No default, no derivation from `SECRET_KEY` (insecure dev default) — a forgotten env var must fail loudly (`ImproperlyConfigured`). Fernet ciphertext is non-deterministic, so this column can never be a lookup/filter key.

### D4. Provider construction: direct, credential-bound, no registry

The existing `CaptchaSolver` pattern (`apps/auto_apply/captcha/base.py`) uses a settings-keyed registry with zero-arg construction, because CAPTCHA-solver config is global. An inbox provider's config is *per-user* — a settings-keyed factory has no way to know "which user?"

**Decision:** a `Protocol` (`EmailCodeProvider.get_code(*, since, deadline_monotonic) -> CodeLookupResult`) with one concrete class, `ImapEmailCodeProvider`, constructed directly in `tasks.py` via `build_email_code_provider(user)` (looks up the active credential, returns the provider or `None` — fail closed, same posture as `get_solver()`). No registry — premature over-abstraction for a single implementation (the same over-abstraction already flagged as a P2 finding against this codebase's LLM provider registry).

### D5. Outcome modeling: one exception carrying an outcome enum, not six exception classes

**Decision:** one new class, `GreenhouseFormVerificationFailed(GreenhouseFormError)`, carrying `outcome: VerificationOutcome` (a small str-enum). `tasks.py`'s existing `_reason_code_for()` gains one branch reading `exc.outcome` through a dict. These six outcomes are variants of one subsystem's result, all handled identically by the call site — six sibling exception classes would repeat the LLM-provider-registry over-abstraction. The dict mapping is totality-testable (one test asserts every outcome has a reason code).

### D6. The failure taxonomy

| Outcome | Reason code | Meaning | Deactivates credential? |
|---|---|---|---|
| `NO_INBOX_CREDENTIALS` | `no_inbox_credentials` | Interstitial hit, no active credential on file | n/a |
| `CODE_TIMEOUT` | `verification_code_timeout` | Polled to budget, no matching email | No |
| `INBOX_AUTH_FAILED` | `inbox_auth_failed` | Login rejected (revoked/wrong app password) | **Yes** |
| `INBOX_UNAVAILABLE` | `inbox_unavailable` | DNS/TLS/socket/server error — not a credential problem | No |
| `CODE_AMBIGUOUS` | `verification_code_ambiguous` | ≥2 candidate emails with differing codes | No |
| `CODE_REJECTED` | `verification_code_rejected` | Code typed, page still shows the verification form | No |

The `INBOX_AUTH_FAILED` vs `INBOX_UNAVAILABLE` split matters for R7: only a genuine auth rejection deactivates the credential — a transient network blip must not force the user to re-enter their app password.

### D7. Ambiguity is fail-closed, plus a per-user serialization lock

A candidate set is unambiguous only if all in-window matching messages yield the *same* code; otherwise → `CODE_AMBIGUOUS`, nothing typed. `submit_auto_apply_draft` acquires a short-lived per-user Redis lock (`cache.add`, already used elsewhere in this codebase for debounce tokens) before entering the verification wait, released in a `finally` — makes the ambiguous case rare rather than routine.

---

## System-Wide Impact

- **`apps/accounts`**: new `crypto.py`; new `EmailInboxCredential` model + migration; admin registration (password excluded from admin display).
- **`apps/auto_apply`**: new `email_verification/` package (Protocol, extraction logic, IMAP provider); `greenhouse_form/exceptions.py` + `client.py` changes; `models.py` `ReasonCode` additions + migration; `tasks.py` time-budget restructure and provider wiring; new `checks.py` for the budget-ordering system check.
- **`apps/web`**: new credential form/views/urls/template; new reason-code messages.
- **`config/settings/base.py`**: `CREDENTIAL_ENCRYPTION_KEYS`, `AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS`, `AUTO_APPLY_VERIFICATION_SENDER_ALLOWLIST`, `AUTO_APPLY_IMAP_ALLOWED_HOSTS`, raised `AUTO_APPLY_SENDING_TIMEOUT_SECONDS`, `CELERY_TASK_ROUTES`.
- **`requirements/base.txt`**: `cryptography` — the only new dependency.
- **Ops**: generate and provision `CREDENTIAL_ENCRYPTION_KEYS` (backed up separately from the DB); stand up the dedicated submit queue/worker.

---

## Implementation Units

### U1. Secret encryption primitive

**Goal:** An explicit, rotatable encrypt/decrypt pair for at-rest secrets, with no implicit decryption anywhere.

**Requirements:** R3 | **Dependencies:** none

**Files:** create `apps/accounts/crypto.py`; modify `config/settings/base.py` (`CREDENTIAL_ENCRYPTION_KEYS`); modify `requirements/base.txt` (`cryptography`); create `apps/accounts/tests/test_crypto.py`.

**Approach:** `encrypt_secret()`/`decrypt_secret()`, raising `ImproperlyConfigured` when the key list is empty and a typed `SecretDecryptionError` on a bad token. Build `MultiFernet` lazily per call (not a module-level singleton) so `@override_settings` works in tests. Add a `generate_key()` helper and a `django.core.checks` warning when unset with `DEBUG=False`.

**Patterns to follow:** `env(...)`/`env.list(...)` idiom and banner-comment blocks in `config/settings/base.py`.

**Test scenarios:** round-trip; non-determinism (two encryptions of the same plaintext differ); key rotation via `MultiFernet` (`[B,A]` still decrypts what `A` encrypted, `[B]` alone raises); unconfigured → `ImproperlyConfigured`; tampered ciphertext → `SecretDecryptionError`, never garbage; settings read per-call (regression guard against a module-level-singleton mistake).

**Verification:** test suite green; `generate_key()` output is accepted by `Fernet`.

---

### U2. `EmailInboxCredential` model

**Goal:** One per-user credential row, ciphertext at rest, with a single explicit mutator and a single explicit deactivator.

**Requirements:** R2, R3, R7, R8 | **Dependencies:** U1

**Files:** modify `apps/accounts/models.py`; create migration; modify `apps/accounts/admin.py`; modify `config/settings/base.py` (`AUTO_APPLY_IMAP_ALLOWED_HOSTS`); create `apps/accounts/tests/test_email_inbox_credential.py`.

**Approach:** `OneToOneField(AUTH_USER_MODEL, related_name="email_inbox_credential")`, mirroring `Profile`. Fields: `email_address`, `imap_host` (validated against the allowlist), `imap_port` (default 993, TLS-only), `app_password_encrypted` (ciphertext, never indexed/filtered), `is_active`, `last_error_code` (a code, not a message — server text may echo the username), `last_verified_at`/`last_error_at`, `created_at`/`updated_at`. Two mutators mirroring `Profile.set_resume()`'s "one visible call site, no signal" idiom: `set_app_password(raw)` (strips whitespace — Google renders app passwords as `abcd efgh ijkl mnop`, and every user's first attempt fails without this; encrypts, reactivates, saves with `update_fields`) and `mark_auth_failed(error_code)` (the sole R7 mutator). `__str__` returns only the username. Admin excludes the ciphertext column entirely.

**Patterns to follow:** `Profile` model shape; `client.py`'s host-allowlist precedent.

**Test scenarios:** round-trip with space-stripping asserted; ciphertext-at-rest check via raw SQL (the load-bearing R3 assertion); reactivation clears `last_error_code`; `mark_auth_failed` sets `is_active=False`; OneToOne uniqueness; cascade delete; host allowlist accepts/rejects; `str()` never contains the address.

**Verification:** migration clean; tests green; admin detail page renders with no password field.

---

### U3. IMAP code retrieval provider

**Goal:** Given a credential and a time window, return a verification code or a typed non-result — never raising into the caller, never logging content.

**Requirements:** R4, R6, R7, R9 | **Dependencies:** U1, U2

**Files:** create `apps/auto_apply/email_verification/{__init__.py, base.py, extraction.py, imap_provider.py}`; modify `config/settings/base.py` (poll timeout/interval, sender allowlist); create `apps/auto_apply/tests/{test_email_code_extraction.py, test_imap_email_code_provider.py}`.

**Approach:** `base.py` defines `VerificationOutcome` (str-enum: `FOUND, NO_INBOX_CREDENTIALS, CODE_TIMEOUT, INBOX_AUTH_FAILED, INBOX_UNAVAILABLE, CODE_AMBIGUOUS`), `CodeLookupResult` (frozen dataclass, `__repr__` redacts the code), and the `EmailCodeProvider` Protocol. `extraction.py` holds **pure, no-I/O** matching functions — the safety-critical logic: candidacy requires sender-domain allowlist match **AND** arrival strictly after `since` (parsed message date, since IMAP's `SINCE` is day-granular) **AND** contextual phrasing match, with the code pulled via a contextual regex (e.g. `(?:code|is)[^0-9]{0,20}\b(\d{6})\b`), never a bare 6-digit scan. `imap_provider.py` implements connect→login→select(readonly)→poll loop (`NOOP()` before every `SEARCH()` — see D2), handling multipart/HTML bodies and encoding failures per-message (skip, don't abort). Only structured outcome lines are logged — never code, subject, body, address, or host, and never a raw `imaplib` exception (its text can echo the login).

**Execution note:** capture one real Greenhouse verification email before finalizing the sender allowlist and phrasing patterns — sender/phrasing are unknown and must not be guessed from a screenshot (mirrors the `_CONFIRMATION_TEXT_PATTERNS` precedent in this codebase, which needed a live correction after an initial assumption was wrong).

**Patterns to follow:** `captcha/base.py` (dataclass + Protocol, `None`/negative = fail closed); `client.py`'s bounded-deadline poll loop.

**Test scenarios:** *(extraction, pure/fast)* realistic body → correct code; multipart HTML → code extracted after tag-stripping; phone-number-shaped body with no verification phrasing → no match; code + unrelated 6-digit number → contextual regex picks correctly; sender not allowlisted → never a candidate even with perfect phrasing/code (the exfiltration guard); pre-`since` message excluded; same code across 2 candidates → `FOUND`; differing codes → `CODE_AMBIGUOUS`. *(provider, mocked IMAP, injected clock — must run in milliseconds, no real sleeps)* happy path with `NOOP` called before `SEARCH`; `BODY.PEEK[]` + `readonly=True` asserted; timeout path; auth failure → `INBOX_AUTH_FAILED` + credential deactivated; connect error → `INBOX_UNAVAILABLE` + credential **not** deactivated; mid-poll transient abort → bounded retry then `INBOX_UNAVAILABLE`; malformed message skipped, poll continues; fake provider satisfies the Protocol (`assertIsInstance`); log capture over a full run contains no secret material.

**Verification:** unit suite green with zero network I/O; one manual run against a real Gmail account before U6 is considered done.

---

### U4. Credential management UI

**Goal:** Add, replace, verify, and remove inbox credentials from the profile page, with immediate live feedback.

**Requirements:** R2, R7, R8 | **Dependencies:** U2, U3

**Files:** modify `apps/web/forms.py`, `views.py`, `urls.py`; modify/create templates; create `apps/web/tests/test_email_inbox_credential_views.py`.

**Approach:** a separate page/route (not more `ProfileForm` fields — different sensitivity and lifecycle). `ModelForm` with `app_password` as a non-model `CharField(widget=PasswordInput(render_value=False))`, routed through `set_app_password()` in an overridden `save()` — direct precedent is `ProfileForm.save()` routing `resume` through `Profile.set_resume()`. Blank password on edit leaves the stored value untouched. **Live verification on save**: in `clean()`, open a bounded (~10s) IMAP connection and log in/out; failure raises a `ValidationError` with actionable copy ("make sure this is an App Password, not your account password"). Warn (don't block) when the credential's address differs from `Profile`'s application email. **Lead with the dedicated-inbox recommendation** (per Decisions Resolved #2): copy frames a throwaway/dedicated Gmail account as the safer default, with "use your main inbox" presented as the less-safe convenience option.

**Patterns to follow:** `views.profile` GET/POST ModelForm shape; `@login_required`/`@require_POST`.

**Test scenarios:** anonymous GET redirects; valid save (mocked IMAP success) creates row, `is_active=True`; save with mocked IMAP failure re-renders with a field error, no row created, no host/exception text in the response; app-password whitespace stripped; blank-password edit preserves stored value; edit with a new working password reactivates; remove deletes the row (repeat remove 404s, not 500s); ownership scoping (user B can't touch user A's credential); rendered HTML never contains plaintext or ciphertext (asserted on response body for both save and error paths); mismatch warning shows but doesn't block.

**Verification:** full browser round trip against a real Gmail app password: save → verified status shown → remove.

---

### U5. Detect the verification interstitial

**Goal:** `submit()` recognizes the post-submit verification page as a distinct typed outcome.

**Requirements:** R1 | **Dependencies:** none (parallelizable with U1-U4)

**Files:** modify `exceptions.py` (new `GreenhouseFormVerificationFailed`), `client.py`; new fixture; modify `test_greenhouse_form_client.py`.

**Approach:** restructure the success-confirmation poll into a three-way classifier (success / verification-required / neither-yet) evaluated once per pass under the existing shared deadline. Detection requires **both** a code-entry control signature **and** confirming copy — two independent signals, because a single one is cheap to false-positive on. **Success wins ties** — check the success signal first each pass. Same class of bug as the reCAPTCHA-v3 false positive already fixed once in this codebase.

**Execution note:** capture the live interstitial's DOM before finalizing selectors — exact markup is unknown, the original report was a screenshot only.

**Patterns to follow:** `_challenge_detected`'s selector-based detection; `_CONFIRMATION_TEXT_PATTERNS`'s multi-word-phrase precedent.

**Test scenarios:** verification fixture → `GreenhouseFormVerificationFailed` (no provider injected); normal success fixture → success, not misdetected; success copy containing "check your email" + a stray numeric input → still success (tie-break); validation-error fixture → unchanged `GreenhouseFormSubmissionFailed`; numeric input with no verification copy → `SUBMISSION_FAILED`; worst-case timing stays within the existing budget, not double.

**Verification:** all existing + new client tests green; live read-only confirmation the selector matches the real interstitial.

---

### U6. Wire the provider into `submit()` and complete the flow

**Goal:** On detection, poll via the injected provider, type the code into the live page, submit, and confirm — or fail closed with a specific outcome.

**Requirements:** R1, R4, R5, R6, R9 | **Dependencies:** U3, U5

**Files:** modify `client.py`; modify `test_greenhouse_form_client.py`; new fixtures.

**Approach:** `GreenhouseFormClient` gains `email_code_provider` (constructor + per-call override, paralleling `captcha_solver`) and `deadline_monotonic`. Record `submitted_at` immediately *before* the submit click (the `since` boundary — capturing it after the click could filter out a fast-arriving email). On detection: no provider → `NO_INBOX_CREDENTIALS` immediately, no polling; compute the remaining poll slice against the deadline, failing closed immediately if too little remains; call the provider wrapped in try/except (a provider bug must never escape as an untyped exception); non-`FOUND` → raise with that outcome; `FOUND` → fill the code, submit, re-run the success poll under the remaining deadline — success returns normally, still-on-verification-page → `CODE_REJECTED`. **R9 carve-out:** debug-artifact capture must be suppressed for every raise on the post-code path (the one path where a screenshot would contain a live OTP) — an explicit, commented trade-off. The code must never appear in an exception message; `str(exc)` per outcome must be a fixed string (no interpolation of host/address/code), since `tasks.py` writes it straight into the user-facing `error_message`.

**Patterns to follow:** existing CAPTCHA-challenge handling and its reason-code mapping.

**Test scenarios:** happy path (code found → filled → success); no provider → immediate `NO_INBOX_CREDENTIALS`, provider never touched; each outcome (timeout/auth-failed/unavailable/ambiguous) maps 1:1, parameterized so a new unmapped outcome fails the suite; provider raising an arbitrary exception → `INBOX_UNAVAILABLE`, not an escape; code typed but still on verification page → `CODE_REJECTED`; `since` passed to the provider is at/before the submit click (frozen clock); near-exhausted deadline → immediate timeout, provider never called; with `debug_artifact_dir` set, a post-code failure writes no files while a pre-code failure still does (no regression); every verification outcome's `str(exc)` contains no digits-of-code/host/address; existing CAPTCHA/schema-mismatch paths unaffected.

**Verification:** full suite green; one live end-to-end run against a real board known to show the interstitial, using a real test Gmail account, ending in a genuinely accepted application.

---

### U7. Task wiring, reason codes, and the time-budget restructure

**Goal:** `submit_auto_apply_draft` constructs the provider, enforces a runtime-computed deadline, serializes verification per user, and maps every outcome to a distinct reason code.

**Requirements:** R5, R6, R7 | **Dependencies:** U2, U3, U6

**Files:** modify `models.py` (6 new `ReasonCode` values + migration), `tasks.py`, `config/settings/base.py`; create `apps/auto_apply/checks.py`; modify `apps.py`, `test_tasks.py`; create `test_submit_time_budget.py`.

**Approach:** add the six reason codes (a no-op at the DB level beyond `choices=`, no data migration, no constraint change — no `Status` change, so `ACTIVE_STATUSES`/uniqueness constraint stay untouched). Budget: delete the old import-time constants; add `_SWEEP_SAFETY_MARGIN_SECONDS` and `_SUBMIT_HARD_KILL_SECONDS` as literal constants; add `_submit_budget_seconds()` reading settings at call time; decorator drops to `time_limit=_SUBMIT_HARD_KILL_SECONDS` only (no `soft_time_limit`); task body computes the deadline and passes it through. Provider + lock: `build_email_code_provider(draft.user)`, acquire the per-user Redis lock only if a provider exists, release in `finally`, pass `email_code_provider=`/`deadline_monotonic=` alongside the existing `captcha_solver=get_solver()` call. `_reason_code_for` gains the new exception branch. A comment at the `error_message = str(exc)` line points at the guarantee (U6) that makes this safe. System check fails startup if the budget ordering is inverted. Add `CELERY_TASK_ROUTES` entry routing `submit_auto_apply_draft` to a dedicated queue.

**Test scenarios:** `_submit_budget_seconds()` honors `@override_settings` (the explicit regression test for the import-time-freezing bug); budget < sweep threshold < hard kill, asserted directly; system check fires on an inverted config; each `VerificationOutcome` → matching `ReasonCode`, `JobApplication` untouched, parameterized totality check; no credential → provider is `None`, draft fails `no_inbox_credentials`; inactive credential treated as absent; auth failure survives the task boundary (`credential.is_active is False` after the task, not just after the provider call); lock contention fails closed; lock released after both success and failure; happy path still upserts `JobApplication` to Applied and respects the existing sweep-race guard; `error_message` for every verification outcome contains no host/address/digit sequence; existing CAPTCHA/schema-mismatch/stale-job/not-SENDING tests unchanged.

**Verification:** `apps.auto_apply`/`apps.web` suites green; migrations clean; `manage.py check` passes on default settings, fails on a deliberately inverted budget.

---

### U8. User-facing messaging and settings documentation

**Goal:** Every new failure state has an actionable message; all configuration is documented in house style.

**Requirements:** R6, R7, R8 | **Dependencies:** U4, U7

**Files:** modify `apps/web/views.py` (reason-code messages), `templates/web/auto_apply_queue.html`, `config/settings/base.py` (consolidated banner comment), `apps/web/tests/test_auto_apply_views.py`.

**Approach:** every new reason code gets a specific, actionable message that leaks nothing (no host, no address, no internal vocabulary like "IMAP"): `no_inbox_credentials` links to the credential page; `inbox_auth_failed` names the likely cause (revoked app password) and links to update it; `inbox_unavailable`/`verification_code_timeout`/`verification_code_rejected` are simple retry prompts; `verification_code_ambiguous` explains briefly and prompts retry. Rewrite `AUTO_APPLY_SENDING_TIMEOUT_SECONDS`'s existing comment, which becomes false once the value changes.

**Test scenarios:** every new reason code renders its specific message, not a generic fallback; the queue page links to the credential route for the two credential-related codes; no message text contains a host, address, or internal vocabulary.

**Verification:** queue page renders correctly for each new state.

---

## Security Assessment

**The trade, stated plainly:** a Gmail app password is not a narrowed credential — it's a full-account password that bypasses interactive 2FA and authorizes IMAP, POP, *and* SMTP (read, delete, **send-as-the-user**). That's strictly broader than `gmail.readonly` OAuth would have been. This codebase is now storing what is arguably the highest-value credential it will ever hold — email is the root of most "forgot password" flows. Encryption at rest defends against exactly one threat class (a stolen DB backup or a SQL-injection read) — a full compromise of the app/worker tier yields the encryption keys too, and therefore every stored password. That's the honest limit of the cryptographic claim; the mitigation that actually caps blast radius is not cryptographic.

**Required mitigations (all included in the units above):** the credential is used in exactly one place (`submit_auto_apply_draft` → the IMAP provider) and nowhere else — no second consumer, ever; decryption is only ever explicit, never a transparent field; the ciphertext is excluded from admin entirely; the code/address/host never appear in logs, exceptions, or `error_message`; debug-artifact capture is suppressed on the post-code path; the IMAP host is allowlisted and TLS/port-993-only; the encryption keys live outside the DB and outside the same backup stream as the database.

**Recommended mitigation, adopted per Decisions Resolved #2:** recommend users connect a dedicated, throwaway inbox rather than their primary email. Sharply caps blast radius (a compromised dedicated inbox resets nothing else) and reduces ambiguity (a near-empty inbox rarely has 2 candidate emails in-window).

**Deferred:** KMS envelope encryption; moving to OAuth once/if the CASA path becomes viable (the Protocol seam makes this a one-module swap later); a general privacy-disclosure mechanism (already flagged as missing on the original auto-apply plan — inbox credentials make that gap materially worse and it should be escalated, not re-deferred silently).

---

## Risks & Open Questions

- **Workspace tenants can disable app passwords entirely** — for those users this feature simply cannot work; no workaround short of OAuth. Mitigated by live verification at setup time. Unknown: how large is the affected user share.
- **Onboarding friction** — asking for an email password *looks* like phishing regardless of legitimacy. Mitigated by live verification, explicit "exactly what we read and why" copy, and prompting contextually only when a draft actually needs it.
- **App passwords go stale silently** on a Google password change — handled as a first-class state (R7), but the user only learns about it when an application fails.
- **Unverified live markup/sender** — both the interstitial DOM (U5) and the verification email's sender/phrasing (U3) are unconfirmed; this codebase has twice already had a reasonable-looking assumption about Greenhouse's rendered pages turn out wrong once tested live. Budget real capture time before finalizing either unit.
- **Code expiry vs. poll budget** — if Greenhouse's codes expire faster than the poll budget allows, a slow email could produce an already-dead code, surfacing as a misleading `CODE_REJECTED`. Determine the actual expiry during live capture.
- **Worker capacity** — up to ~9 minutes per submit task holding a Chromium process changes capacity planning for concurrent auto-applies (mitigated by the dedicated queue).
- **Non-Gmail providers** (Outlook/Office365 etc.) are in the allowlist shape but unvalidated — don't advertise beyond Gmail until validated.

---

## Test Strategy

Django's built-in test runner (no pytest in this repo). `SimpleTestCase` for pure logic (extraction rules, crypto), `TestCase` for anything touching the DB. Mocks via `unittest.mock.patch` at the *consuming module's* import site (this codebase's established convention), plus hand-rolled fakes for the new `EmailCodeProvider` Protocol mirroring the existing `FakeCaptchaSolver` pattern (including a contract test asserting a fake satisfies the `runtime_checkable` Protocol). No new dev dependency for IMAP mocking — hand-rolled fakes only, and the provider-layer tests must inject the clock/sleep so a "timeout" test runs in milliseconds, not the real poll duration.

Cross-cutting assertions required somewhere in the suite: totality (every outcome has a reason code and a message); secret non-leakage (log capture + exception-string assertions across every new failure path); artifact suppression on the post-code path; the budget-ordering invariant (both directly asserted and enforced by a system check); ciphertext-at-rest via raw SQL.

Manual/live verification gates: capture one real verification email before finalizing U3's sender/phrasing; a real Gmail app password round-trips through U4's save/verify/remove; capture the live interstitial DOM before finalizing U5's selectors; one full live submission against a board known to show the interstitial before U6/U7 are considered done.
