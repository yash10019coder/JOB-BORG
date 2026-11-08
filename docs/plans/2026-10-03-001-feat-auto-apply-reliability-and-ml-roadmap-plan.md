---
title: Auto-apply reliability roadmap - API-schema-first drafting, success confirmation, observability, ML assist
type: feat
status: active
date: 2026-10-03
---

# Auto-apply reliability roadmap

## Summary

Auto-apply (Greenhouse, Playwright-driven) took about two months and five distinct live bugs to reach its first end-to-end successes. This plan records why it is brittle, what already exists that can be adopted instead of built, and a staged roadmap: deterministic fixes first (which also produce the labelled data and measurement ML needs), then small adopted ML/NLP components in shadow mode.

Evidence comes from the production database (read-only, one user), git history, a code/test/ops audit, and web research verified against primary sources (a live API call, official docs, repositories). Items that could not be verified are labelled **UNVERIFIED**. Caveat: single user, only 33 jobs ever reached a send attempt, so ratios are indicative, not statistical.

## Tracking (GitHub issues)

Parent bucket: #17. Epic: **#104**.

| Issue | Scope |
|---|---|
| #104 | Epic: auto-apply reliability |
| #105 | Step 0 deterministic verification wins (children: #79 code extraction + inbox retrieval, #53 recency filter bypass, #52 reason-code misuse) |
| #106 | API-schema-first drafting via Greenhouse `?questions=true` (child: #62 duplicated `_discover_schema` logic) |
| #107 | Out-of-band success confirmation (inbox reconcile + network/URL signals) |
| #108 | Always-safe artifacts, redacted snapshots, tracing, Prometheus metrics |
| #109 | Dry-run mode, weekly canary, CI installs Chromium, worker auto-restart |
| #110 | Supported-board gate + validated, chunked LLM calls |
| #111 | ML/NLP assist (children: #112 resume facts, #113 answers bank + option matching, #114 shadow detectors + groundedness) |

Related, not re-parented: #55 (`client.py` size; deferred refactor), #57, #59, #60 (verification-flow gaps), #16 (answers_bank schema), #82-#84 (CI), #100 / #101 / #102 and PR #103 (UI work in flight elsewhere).

## Diagnosis

- Only 4 of 251 drafts ever reached `applied`; of the 33 jobs that reached a send attempt, 4 succeeded (~12%), and 3 of those 4 needed 2-7 failed retries first. All four successes are from the last two days of data.
- About 40% of failures are "outcome unknown" (~24 of 61): no post-submit success signal (16), code entered but unconfirmed (5), sending timeout (3). Success is decided only by 8 page-text phrases (`_CONFIRMATION_TEXT_PATTERNS`, `apps/auto_apply/greenhouse_form/client.py:137`); no network-response, URL, or inbox signal exists.
- The feedback loop is live-only: 29 hand-written HTML fixtures (2 derived from real captures, 14 with no provenance), CI never installs Chromium so ~75 browser tests are silently skipped (`.github/workflows/ci.yml:46-52`), no dry-run or canary, and 163 real captures sit unused in a gitignored folder.
- Failures are black boxes: artifacts are suppressed after OTP entry, there is no step tracing and no auto-apply metrics, and reason codes are inconsistent (the same message is filed under two codes; `VERIFICATION_BUSY` and `CODE_AMBIGUOUS` are never assigned; `GreenhouseFormSubmissionUnconfirmed` is defined twice in `exceptions.py`).
- `client.py` is 1,965 lines with 51 functions and 52 inline locators; 28 of its 38 commits since August are fixes, and the Verify-button heuristic has been patched three times.
- 23% of drafts are excluded at draft time (16 `form_load_failed`, mostly non-Greenhouse hosts that still offer Auto-apply); four employers account for 32 of 61 failures.
- The LLM layer drives review burden: 28% of answer slots have no usable LLM output (one failed batched call blanks every question), only 8% are `ok`, 79% of drafts have at least one `needs_review` field, and answers are not deterministic (a time-zone answer flipped between drafts).
- Browser automation was the right call (the submit API needs a per-employer key; see `docs/brainstorms/2026-08-02-auto-apply-greenhouse-slice-requirements.md:110`), but its accepted fragility was not matched by investment in guardrails.

## Research findings: what already exists

| Need | What exists | Verdict |
|---|---|---|
| Field list, types, required flags, options | **Greenhouse public Job Board API** `GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`, no key. **Verified live on Canonical's job**: 22 questions; each `name` equals the DOM control id we already scrape (e.g. `question_45893749`, `first_name`); type (`input_text`, `textarea`, `input_file`, `multi_value_single_select`, `multi_value_multi_select`), `required`, full option lists (314 countries, 226 nationalities), and an EEOC `compliance` block with exact option labels (gender, race, veteran, disability). Gaps seen: Country/phone widget, School/Degree/Discipline (`education: education_optional`), `hispanic_ethnicity`. | **Adopt as source of truth** |
| Why the email code appears | Greenhouse docs: shown "depending on your spam sensitivity setting and the user's score" (invisible reCAPTCHA, employer-chosen); no documented off switch. Headless / devtools-driven browsers score badly (please-hire-me README). | Our headless setup likely causes it |
| Code email format and entry | Sender `no-reply@us.greenhouse-mail.io`; body "...security code field on your application: <8-char code>"; subject "Security code for your application to <company>"; codes can be letters-only; 8 inputs `#security-input-0..7` (issue #79; OSApplyTrack PRs #165/#170; lavorai PR #17). Our extractor requires a digit and a 25-char gap (real gap is 28). | **Adopt as deterministic rules** |
| Success detection | Submit network response (reported: HTTP 428 `{"code":"captcha-failed","security_code_recipient":...}` for the code screen, success status otherwise), redirect to a `/confirmation` URL, and the employer "Thank you for applying" email (configurable, may be off). Network/URL details come from repo PRs: **UNVERIFIED** against Greenhouse docs. | Adopt as signals after a passive capture confirms them |
| Confirmation-email patterns | amimrjo/Job-Application-Tracker (MIT) rule patterns (`thank you for (applying\|your application)`, `we('\| ha)ve received your application`, `your application (has been\|was) (received\|submitted)`); its ATS domain list lacks `greenhouse-mail.io`. | Adapt patterns; SetFit only if rules miss |
| Page-state / OTP-input / field-role models | No pre-trained model fits. Mozilla Fathom is archived (2025-11); Chrome's ML field model is not licensed for reuse; MarkupLM/MindAct unsuitable; Mind2Web/WebLINX are research-only or non-commercial. Reusable: HTML `autocomplete="one-time-code"`, Chromium `legacy_regex_patterns.json` and one-time-code parser (BSD-3), SetFit few-shot. | Deterministic first; small classifier as fallback/shadow |
| Option matching, answers bank, embeddings | RapidFuzz (MIT); fastembed (Apache-2.0, ONNX/CPU); bge-small-en-v1.5 (384-dim, matches the existing `Job.embedding` column; MIT per recollection, UNVERIFIED); pgvector HNSW. Simplify stores exact-match answers only. | Adopt |
| Groundedness of LLM answers | Vectara HHEM-2.1-open (Apache-2.0, ~0.1B params, CPU-capable, needs `trust_remote_code` so pin the revision); cross-encoder/nli-deberta-v3-small. | Adopt in shadow |
| Resume facts | Docling (MIT) for PDF/DOCX text + one LLM call into a JSON-Resume-shaped Pydantic schema; compute years in Python. OpenResume (AGPL) and pyresparser (GPL) unsuitable. | Adopt / adapt |
| LLM reliability | Instructor (MIT): retry with the validation error; chunk 5-8 questions; retry only failed slots. | Adopt |
| Self-healing / cached selectors | Stagehand (MIT): cache action -> selector, re-infer only on a miss (reported 50-100 ms hit vs 1-5 s). Playwright MCP (Apache-2.0) for offline triage. | Copy the pattern; keep LLMs off the hot path |
| End-to-end email testing | Mailpit (MIT) | Adopt for tests |
| Avoid | Skyvern and ApplyPilot (AGPL); invisible_playwright (built to evade bot detection); workflow-use (AGPL, unfinished). | Do not use |

Design signals from the best existing tools (OfferOS, please-hire-me, job-apply-plugin): read the form's machine-readable field descriptions rather than visible labels (OfferOS reports question accuracy 37.5% -> 100%); never auto-answer self-ID / work authorization / sponsorship / salary (matches our policy); label saved answers confirmed / inferred / sensitive (matches our `user_confirmed` marker); record "blocked" and "unknown" as distinct outcomes; screenshot every submission.

## Product and risk decision (open)

- The well-reviewed tools run inside the user's real browser and leave the final click to the human; server-side mass-apply tools draw ban, spam and wrong-answer complaints. Greenhouse actively targets bot-like volume (fraud detection, Real Talent bot flags; a reported case of 8 applications in 2 minutes flagged as spam). The MyGreenhouse user agreement bans "automated means" for its Services (whether that covers public application forms is unclear).
- Posture to keep: low volume, every send already human-clicked, no stealth or evasion tooling, no alternate email address.
- Whether to also move to a headed / real-browser or hybrid "assisted apply" mode is an open product call. Everything below works in any mode. Instrument whether the code screen appears per run so the effect of any mode change is measurable.

## Roadmap

### Step 0 - deterministic quick wins (#105)

1. Implement #79 in `apps/auto_apply/email_verification/extraction.py` and `imap_provider.py`: template-anchored extraction (token after "application:", 6-10 alphanumerics, digit not required); accept sender `greenhouse-mail.io` (+ `greenhouse.io`) and subject "Security code for your application to"; narrow the IMAP search to that sender since submit; do not re-download examined UIDs; reconnect once on abort. Fixtures: the two real emails from #79 (codes swapped); Mailpit for an end-to-end test.
2. Deterministic OTP input location: try `#security-input-0..7`, then `autocomplete="one-time-code"`; keep the existing selector (`client.py:173`) as fallback (`_fill_verification_code`, `client.py:1854`).
3. Passive capture on every real submit (no behaviour change): log submit-response URL/status/JSON keys (values redacted) via `page.on("response")`, plus the final URL, to verify the 428 / `/confirmation` hypothesis from our own runs.
4. Taxonomy cleanup: duplicate `GreenhouseFormSubmissionUnconfirmed` (`exceptions.py:47,54`); assign or delete `VERIFICATION_BUSY` / `CODE_AMBIGUOUS` (#52); recency filter (#53).

### API-schema-first drafting (#106) - biggest structural win

- DB-free `GreenhouseQuestionsClient` beside `apps/jobs/ingestion/greenhouse_client.py` (injected session, fail closed) fetching `?questions=true` for a job's board token + job id; build the `FormSchema` (`greenhouse_form/field_mapping.py`) from it, including the EEOC block, keyed by the same control ids.
- `draft_for` (`services/drafting.py`) uses it instead of a Playwright `inspect()`: drafting becomes one HTTP call, with authoritative required/type/options (no 314-country scrolling, no required-attribute misdetection such as the 9 empty Cover Letter failures). Playwright is kept only for DOM-only fields (Country/phone widget; School/Degree/Discipline via the existing `education_api.py`) and as the submit-time drift check (mismatch stays `schema_mismatch`).
- Parity test first: for the 116 boards in stored `form_schema_snapshot`s, fetch the API and diff names/types/required/options against the DOM-derived snapshot; report the mismatch rate before switching. Fall back to Playwright `inspect()` on a 404 or unknown token.
- Side benefit: a browser-free corpus of question labels/options (from the 210k ingested jobs) for any classifier training.

### Out-of-band success confirmation (#107)

- Pure `apps/auto_apply/email_verification/confirmation.py` (I/O-free, mirrors `extraction.py`): arrived after `SENDING`, names employer/title, received/thank-you phrase, accepts `greenhouse-mail.io`; fail closed.
- Celery beat task `reconcile_unconfirmed_drafts`: `SUBMISSION_UNCONFIRMED` drafts from the last ~48h become `APPLIED` when a matching email exists (same `JobApplication` upsert as `tasks.py:369-380`), lifting the retry guard automatically. A missing email means "unknown", never failure.
- Add the submit-response and `/confirmation` URL as structural success signals once Step 0.3 confirms them; page text stays as fallback.
- Prerequisite: 2-3 real, redacted confirmation emails as fixtures. No guessed patterns (the recurring lesson in this codebase).

### Always-safe artifacts, snapshots, tracing, metrics (#108)

- `page.screenshot(mask=[page.locator("input, textarea")])` on every failure path (replace the blanket post-OTP suppression, `client.py` ~1587-1591); no `aria_snapshot()` after OTP (it contains values).
- Snapshot-first capture: one `page.evaluate` returning a redacted pure-data dict (URL, text head+tail, element attributes/labels/button texts, input values removed). Existing a11y captures contain typed PII, so they are not reusable raw.
- Structured step logs and Prometheus counters/histograms (`auto_apply_attempts_total{phase,outcome}`, `auto_apply_step_seconds{step}`, `auto_apply_code_screen_total`), a Grafana panel beside `celery.json`; add `AUTO_APPLY_DEBUG_ARTIFACT_DIR` to `.env.example`.

### Dry-run, canary, CI, dev ergonomics (#109)

- `submit(..., dry_run=True)` (`client.py:338`): fill, assert Submit is enabled, stop before `_click_submit` (line 413).
- `auto_apply_canary` management command (new `apps/auto_apply/management/commands/` package, weekly via beat) over a small board list (Canonical, Alpaca, Atomicwork, Carrallison); never submits.
- CI: `playwright install --with-deps chromium` in `.github/workflows/ci.yml`; Compose `develop.watch` restart action for `worker` / `beat`.

### Supported-board gate + LLM reliability (#110)

- Show Auto-apply only for supported hosts (`DEFAULT_ALLOWED_HOSTNAMES`, `client.py:72`; `templates/web/_job_card.html`, `trigger_auto_apply` in `apps/web/views.py`).
- Instructor-style validated, chunked LLM calls with per-slot retry (`llm/base.py:258`, `llm/langchain_client.py`), temperature 0 where supported, prompt-cached resume prefix.

### ML/NLP assist (#111-#114) - after the above; adopt libraries, no training from scratch

Architecture principles: a pure, versioned leaf module `apps/auto_apply/ml/` (no DB/network/Playwright imports, loaded once via `lru_cache`, per the AGENTS.md convention for `locations` / `classification.engine`); shadow -> assist -> authoritative per detector behind `AUTO_APPLY_ML_<DETECTOR>=off|shadow|on`; every prediction logs model version and probability; a false "success" is the costly error, so "success" requires high precision plus no contradicting structure or inbox corroboration; for hard-excluded categories a classifier may only widen exclusion, never narrow it; low confidence falls back to the rule path or `needs_review`.

- **Resume facts (#112):** Docling + one structured LLM extraction at upload (`Profile.set_resume`), years computed in code; factoid questions answered deterministically.
- **Answers bank + option matching (#113):** pgvector HNSW + fastembed bge-small over `user_confirmed` answers (extends `_carry_forward_confirmed_answers`, `drafting.py:403`); thresholds tuned on real data (suggested start ~0.90 auto-reuse, 0.80-0.90 as LLM context). Option matching for non-EEO fields: RapidFuzz first, then embedding similarity with a margin rule, else `needs_review` (extends `_enforce_option_constraint`, `answer_resolution.py:193`), now on complete API option lists. EEO/demographic categories excluded.
- **Shadow detectors + groundedness (#114):** SetFit (+ Chromium regex JSON as a base) for custom-question category/field role and page-state on unexpected pages; promotion gate (proposed): >=50 real labelled examples per class from >=15 boards, success labels from #107. HHEM-2.1-open scores free-text answers against resume text. `eval_models` harness with a 100%-recall gate on sensitive categories, run in CI.
- New dependencies (pin revisions): `fastembed` / `onnxruntime`, `rapidfuzz`, `setfit` / `scikit-learn`, `docling`, `instructor`.

## Deferred / not recommended

- Fine-tuning a generative model (too little data), a local 3B-class LLM, and VLM canary diagnostics: revisit if hosted-API spend or drift maintenance becomes material.
- Any ML/fuzzy matching for EEO fields; an LLM agent inside the live submit loop; splitting `client.py` into stages (#55) until the safety net above exists.
- UI work (saved-answers page, infinite scroll, modal, queue filters/grouping) is already in flight elsewhere (PR #103); out of scope here.

## Verification

- Step 0: unit tests on the two real Greenhouse emails (digit and letters-only; look-alikes must not match); Mailpit end-to-end; the next real submit logs response status/URL without changing behaviour.
- API-schema-first: parity report over the 116 stored boards; a Canonical draft built from the API matches the existing snapshot; unknown token falls back to Playwright.
- Success confirmation: fixture-email tests (match, wrong employer, too early, ambiguous stays unconfirmed); a reconciled draft becomes `APPLIED`, creates the `JobApplication`, lifts the guard.
- Observability: a post-OTP failure writes a masked screenshot with no typed code; snapshots contain no input values; counters increment in unit tests.
- Dry-run/canary: `docker compose exec web python manage.py auto_apply_canary` on Canonical sends no POST; CI log shows browser tests running.
- Gate/LLM: an unsupported-host job shows no Auto-apply button; one failed LLM chunk does not blank the rest.
- ML (offline first): option-match precision on held-out confirmed answers; sensitive-category recall 100%; share of questions answered without an LLM call; blank-slot rate (28% today) and `needs_review` per draft (6.6 today) on replayed drafts; shadow agreement/precision before any detector is promoted.
- Headline metrics: "outcome unknown" failures fall from ~40% toward ~0; drafting no longer needs a browser; letters-only codes stop timing out.

---

## Appendix A - Production failure data (read-only, 2026-10-02)

Dataset: 251 `AutoApplyDraft` rows, 193 (user, job) pairs, a single user.

**Drafts by status / reason_code**

| n | status | reason_code |
|---|---|---|
| 93 | drafted | - |
| 39 | failed | submission_failed |
| 36 | stale | - |
| 33 | excluded | unanswerable_required |
| 16 | excluded | form_load_failed |
| 9 | failed | schema_mismatch |
| 8 | excluded | schema_mismatch |
| 4 | failed | verification_code_timeout |
| 3 | failed | verification_code_ambiguous |
| 3 | applied | - |
| 3 | failed | sending_timeout |
| 2 | failed | submission_unconfirmed |
| 1 | applied | '' |
| 1 | failed | inbox_unavailable |

**Failed drafts by pipeline phase (reason_code as a proxy)**

| Phase | n | Notes |
|---|---|---|
| Post-submit ambiguity | ~24 | 14+2 "No post-submit success signal", 5 "code entered but not confirmed" (filed under `submission_failed`), 3 `sending_timeout` |
| Form-fill | 19 | 9 empty Cover Letter file; 9 "No matching option" (Location (City) x3, 'Bachelor of Information Technology' x3, Discipline, two LLM prose answers put into a combobox); 1 locator value mismatch (CRLF) |
| Schema drift at submit | 9 | rendered form no longer matches the drafted schema |
| Verification | 8 | `verification_code_timeout` 4, `verification_code_ambiguous` (busy) 3, `inbox_unavailable` 1 |
| Other | 1 | hostname refusal (non-Greenhouse domain) |

**Attempts per job:** 169 jobs had 1 draft; 11 had 2; 6 had 3; 3 had 4; and single jobs had 5, 7, 8, 10. Four jobs ever applied, after 1, 3, 4, and 8 drafts respectively (0, 2, 3, 7 failed drafts first). 29 jobs that reached a send never succeeded.

**Employer concentration (applied / failed / excluded):** Alpaca 0/11/14; Databricks (non-Greenhouse host) 0/0/11; Canonical 1/7/0; Atomicwork 0/7/0; Carrallison 0/7/0; Doitintl 1/3/0; Agoda 1/2/0; Byd 0/3/0. Four employers account for 32 of 61 failures.

**Answer quality:** 2,756 answer entries across 151 drafts with answers (~18.3/draft); 79% of drafts have at least one `needs_review` field (6.6 per draft overall). By `reason`: profile 903, missing_llm_response 451, llm_call_failed 321, insufficient_evidence 279, ok 224, ungrounded_evidence 178, curated_default 130, hard_excluded_category 94, carried_forward_from_previous_draft 78, invalid_option 49, explicit_answer 32. 28% of entries have no usable LLM output; only 8% are `ok`. Only 75 entries are `user_confirmed`.

## Appendix B - Architecture and test/ops audit

- Non-test code in `apps/auto_apply/` is 5,346 lines: `greenhouse_form/client.py` 1,965; `services/drafting.py` 504; `tasks.py` 421; `llm/base.py` 319; `email_verification/imap_provider.py` 277; `llm/langchain_client.py` 268; `services/answer_resolution.py` 227. Tests are 8,590 lines in 14 modules plus 29 HTML fixtures (1,727 lines).
- `client.py` has 16 module-level pattern/selector/timing constants, 52 inline locator calls, and explicit sleeps/timeouts at several points (30 s navigation and confirmation timeouts, 5 s combobox/settle timeouts). `_confirm_success` is ~260 lines interleaving success detection, interstitial handling, code lookup, filling, clicking and polling.
- Git: 38 commits touched `client.py` since 2026-08-01 (28 fix, 3 feat, 1 refactor, 1 revert, 5 merges); 70 commits across `apps/auto_apply`.
- Decision history: the original requirements chose browser automation because Greenhouse's application POST requires a per-employer Job Board API key (`docs/brainstorms/2026-08-02-auto-apply-greenhouse-slice-requirements.md:110`); the plan accepted "higher fragility (DOM drift, bot-detection walls)". Several DOM assumptions later proved wrong (success `role="status"` never appears; the verification input was a single field, then 8 boxes; the code is 8 characters, not 6) and some remain marked UNVERIFIED in `client.py` / `extraction.py`.
- The `answers_bank` was planned (issue #16, "Phase 5") and never built; only the small `ExplicitAnswer` model exists (4 categories).
- Tests: ~72 real-browser tests against fixture HTML (`GreenhouseFormClientTests`, skipped when Chromium is unavailable) and ~34 MagicMock/pure-logic tests in `test_greenhouse_form_client.py`; CI never installs Chromium. 2 of 29 fixtures derive from real captures; 163 real captures exist in gitignored `media/auto_apply_debug/`, none used by tests.
- Observability: debug artifacts only when `AUTO_APPLY_DEBUG_ARTIFACT_DIR` is set (default empty, not in `.env.example`); capture suppressed after OTP entry; logs are free-text JSON shipped to Loki; no auto-apply Prometheus metrics (HTTP and Celery generics only).
- Ops: `web` runs `runserver` (auto-reload) while `worker` runs plain `celery` (no reload); Playwright launches a fresh headless Chromium per call with no stealth/UA/proxy settings; submit hard-kill 900 s, sending timeout 600 s (540 s budget), verification poll 180 s / 4 s.
- LLM: LangChain `init_chat_model` with structured output, all allowed questions in one batched call per draft, no temperature/seed control for most providers, no cache, no cross-job reuse.

## Appendix C - Greenhouse API verification (2026-10-02)

`GET https://boards-api.greenhouse.io/v1/boards/canonical/jobs/5703396?questions=true` (no authentication) returned top-level keys including `questions`, `compliance`, `location_questions`, `demographic_questions`, `education` (`education_optional`), `data_compliance`. 22 questions, e.g. `first_name` (`input_text`, required), `resume` (`input_file`, required), `question_45893749` (`textarea`, required), `question_45893752` (`multi_value_single_select`, 314 options), `question_65008223[]` (`multi_value_multi_select`, 226 options), `question_51726141` (4 options). The EEOC block carried `disability_status`, `veteran_status`, `race` and `gender` with exact option labels. The control ids in our stored DOM-derived schema snapshot for the same job (`first_name`, `question_45893742`, `question_45893749`, `veteran_status`, `gender`, `disability_status`, ...) matched the API `name` values; DOM-only fields were Country, School/Degree/Discipline (`school--0`, `degree--0`, `discipline--0`) and `hispanic_ethnicity`.

## Appendix D - Research sources (verified unless marked)

Auto-apply tools and Greenhouse specifics:
- OSApplyTrack (Apache-2.0), PRs #165 / #170: code screen handling (HTTP 428 `captcha-failed`, `#security-input-0..7`, IMAP subject "Security code for your application to <company>"). github.com/CryptoJones/OSApplyTrack
- lavorai PR #17 (no license): sender `no-reply@us.greenhouse-mail.io`, 10 s look-back, ~180 s wait. github.com/Geraxi/lavorai
- please-hire-me (MIT): real (non-headless) Chrome because reCAPTCHA v3 flags headless; separate Gmail code script. github.com/alecswang/please-hire-me
- OfferOS (Apache-2.0): reads machine-readable field descriptions; never auto-answers self-ID/work-authorization/sponsorship/salary. github.com/averatec0773/offeros
- job-apply-plugin (MIT): confirmed / inferred / sensitive saved answers. github.com/neonwatty/job-apply-plugin
- amimrjo/Job-Application-Tracker (MIT): confirmation/rejection/interview regexes. ApplyPilot (AGPL), invisible_playwright (evasion tooling): avoid.
- Greenhouse reCAPTCHA / spam-sensitivity article: support.greenhouse.io/hc/en-us/articles/115005448066; "Thank you for applying" template: .../115002573326; auto-reply: .../360020268211. Job Board API: docs.greenhouse.io/job-board.html; Candidate Ingestion API (partner-only): docs.greenhouse.io/candidate-ingestion.html.
- Risk sources: MyGreenhouse user agreement (my.greenhouse.io/users/agreement); Greenhouse fraud detection (support article 45397232315035); Fortune coverage 2026-07-27; Hacker News item 42531695.

ML / form understanding:
- Chromium autofill form parsing (BSD-3): chromium.googlesource.com/chromium/src/+/HEAD/components/autofill/core/browser/form_parsing/ (`legacy_regex_patterns.json`, one-time-code parser).
- HTML `autocomplete="one-time-code"` (WHATWG).
- SetFit (Apache-2.0, v1.2.0 2026-09-04), fastembed (Apache-2.0), RapidFuzz (MIT), bge-small-en-v1.5 / gte-small / all-MiniLM-L6-v2 / multilingual-e5-small (384-dim).
- Stagehand (MIT) caching docs: docs.stagehand.dev/v3/best-practices/caching; Playwright MCP (Apache-2.0); browser-use (MIT).
- Not suitable: Mozilla Fathom (archived 2025-11-18), Skyvern (AGPL-3.0), MarkupLM, MindAct, Mind2Web / WebLINX datasets, AgentQL (cloud API), Healenium (Java/Selenium).

Email, answers reuse, resume, LLM reliability:
- Mailpit (MIT) for e2e email tests; Mailosaur / MailSlurp are paid and do not document extraction rules.
- pgvector HNSW; GPTCache (not needed). Simplify "Unique Questions" stores exact-match answers only.
- Vectara HHEM-2.1-open, MiniCheck, cross-encoder/nli-deberta-v3-small for groundedness.
- Docling (MIT), JSON Resume schema (MIT); OpenResume (AGPL) and pyresparser (GPL) rejected.
- Instructor (MIT), Pydantic AI (MIT), Outlines (Apache-2.0).

**UNVERIFIED:** whether employers can fully disable Greenhouse's "Thank you for applying" email and its exact sender/subject; the HTTP 428 / `/confirmation` signals (third-party repo PRs only); when the code step was introduced; the bge-small-en-v1.5 licence text and whether ONNX files ship in the repo; multilingual-e5-small size/prefixes; Bitwarden's licence and TOTP keyword list; CPU latency for every model named (benchmark locally); all cosine/chunk-size thresholds are suggestions to tune on real data; whether the MyGreenhouse "automated means" clause covers public application forms; how Huntr, Teal, Careerflow detect application-received emails.
