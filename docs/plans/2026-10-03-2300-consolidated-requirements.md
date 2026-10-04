---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm + ce-pov
created_at: "2026-10-03T23:00:00Z"
updated_at: "2026-10-04T00:30:00Z"
status: active
artifact_readiness: implementation-ready
scope: "Complete consolidated requirements for JobBorg Profile System overhaul"
---

# Consolidated Requirements: JobBorg Profile System Overhaul

> **Revision note (2026-10-03 22:30 IST).** This version replaces a working copy
> that was truncated mid-FR8. Changes vs. commit `51c2b0f`:
> 1. Restored resume parsing, NFRs, data model, endpoints, phases, tests.
> 2. Aligned vocabulary with the real code: `Profile.VisaStatus` (11 values) and
>    `Profile.salary_by_region` JSON + `preferred_currency` — there are **no**
>    `salary_region_XX` columns.
> 3. Applied the cross-model review (`2026-10-03-2200-synthesized-review.md`):
>    legal-attestation fields (work auth, sponsorship, citizenship) and salary
>    are **never auto-learned or auto-parsed**; custom questions use
>    exact-match-first, embeddings retrieve candidates only; one provenance
>    precedence model governs every writer.
>
> **Revision note (2026-10-04, multi-persona doc review of epic #117).**
> 1. **Tier model changed:** every field is now learnable/importable, including
>    legal (T0) and commercial (T1) answers, but a T0/T1 answer that did not come
>    from an explicit user action is **never submitted unreviewed**. It is
>    prefilled and marked `needs_confirmation`; the user confirms or edits it in
>    the review queue. Only T2 may ever skip review (after the FR8.6 gate).
> 2. Quick-fill now requires an explicit confirm (fixes provenance laundering).
> 3. Resolver re-classifies at resolve time; classifier moved to a leaf module
>    that builds on the existing `QuestionCategory` in `apps/auto_apply/llm`.
> 4. Unit 1 no longer carries the T2 gate; integration point corrected to
>    `answer_resolution.py`; missing units/security requirements added.
> 5. Findings that need a product decision are in **Open Questions**.
>
> **Requirement ID map (old → new):** committed FR7 (learning) → FR8;
> committed FR8 (resume parsing) → FR9; committed FR9 (provenance) → FR7.
> FR1–FR6 unchanged in meaning.

## Problem Frame

Today a job seeker maintains two overlapping pages (Profile and
ExplicitAnswers). Citizenship and minimum-salary fields confuse users, nothing
is learned from past applications, and the uploaded resume is only flattened to
`resume_text`. Auto-apply drafts frequently stall on custom questions
(`unanswerable_required` ≈ 18%), which means users either babysit the review
queue or lose applications.

**Goal:** one coherent profile where user-entered, parsed, and learned answers
converge under explicit provenance, so auto-apply succeeds more often **without
ever submitting a confidently wrong legal or compensation answer.**

Guiding invariant (inherited from `apps.locations.engine` and
`_AUTHORIZATION_BY_VISA_STATUS`): **a confidently wrong answer is worse than an
unanswered one.** When uncertain, leave blank + `needs_review`.

---

## Actors

| ID | Actor | Description |
|---|---|---|
| A1 | **Job Seeker** | Creates profile, manages auto-apply answers, reviews drafts and suggestions |
| A2 | **Auto-apply Engine** | Drafts/submits Greenhouse applications; reads answers **only** via `resolve_answer()` |
| A3 | **Learning Loop** | Observes drafts, review-queue edits and outcomes; proposes `ProfileSuggestion`s |
| A4 | **Profile Importer** | Extracts structured data from resume / LinkedIn PDF / GitHub |

---

## Answer Risk Tiers (governs FR2–FR9)

Risk is assigned by **question semantics**, not by storage field.

| Tier | Examples | Who may propose a value | May be submitted without per-application user review? |
|---|---|---|---|
| **T0 — Legal attestation** | Work authorization, sponsorship, citizenship, criminal history, veteran / disability / EEO, prior employment at this company, background-check consent | User, Learning, Importer | **Never** unless the value is user-confirmed (`source=user`); otherwise prefilled + `needs_confirmation` |
| **T1 — Commercial** | Salary expectation, notice period, start date, relocation willingness, location preferences | User, Learning, Importer | **Never** unless user-confirmed; otherwise prefilled + `needs_confirmation` |
| **T2 — Factual / descriptive** | Years with X, technologies, education, projects, portfolio links, "how did you hear about us" | User, Learning, Importer | `source=learned` only after the FR8.6 shadow-mode gate passes; `source=imported` only after the user accepted it in the FR9.8 review (URL and computed/inferred values follow the same rule) |

**Confirmation rule (governs T0/T1).** A learned or imported T0/T1 value is a
*proposal*. It is prefilled into the draft and flagged `needs_confirmation`; the
submission is blocked until the user confirms or edits it in the review queue.
Confirming is an explicit user action that stores the value as `source=user`
and writes the FR7.6 attestation snapshot. A confirmed value carries over to
later applications until the user changes it (locked user row), so the user
confirms once per question/country, not once per application.

The tier classifier is a dependency-free leaf module (see Open Questions) built
on the existing `QuestionCategory`/`classify` in `apps/auto_apply/llm/categories.py`
rather than a parallel list. T2 requires a **positive** allowlist match; a
question that also matches any T0/T1 pattern takes the higher tier; anything the
classifier cannot place is treated as T0.

---

## Functional Requirements

### FR1: Unified Profile UI (Tabbed)

| ID | Requirement | Priority |
|---|---|---|
| FR1.1 | Tabs: **Profile** (search criteria) \| **Auto-apply Answers** (form-filling data) are P0. The **Learning** (suggestions) tab ships with Phase 4 and is hidden until then | P0 |
| FR1.7 | **Contact & location facts** (found missing by DB check: 27 of 33 `unanswerable_required` drafts involved phone, city, country, address or timezone, and `accounts_profile` has no city/country/address/timezone columns; `phone` exists but is empty on every profile). Add typed Profile facts for city, country, full mailing address and working timezone (directional: new `Profile` columns, entered in Tab 1 or Tab 2), resolved by `resolve_answer()` ahead of AnswerBank like FR2–FR4. User-typed values are T2 and may auto-apply; learned/imported values follow the normal tier rules | P0 |
| FR1.2 | Tab 1 (`/profile/`): target titles, tags, locations, excluded employers, remote preference, `min_salary` + currency (matching filter), headline, active flag, resume upload. **Amended (Phase 2):** contact/link fields (`full_name`, `phone`, the three URLs, `current_employer`) moved to Tab 2 with FR1.7, since they are form-filling facts that matching never reads | P0 |
| FR1.3 | Tab 2 (`/profile/answers/`): contact & location (FR1.7), work-authorization repeater (FR2), citizenship (FR3), salary by region (FR4), custom answers (FR5), questions panel (FR6), and the user's old saved answers (read-only, delete only). Two server-rendered pages with separate submits; saved authorization rows are rendered by the server so the page works without JavaScript | P0 |
| FR1.4 | Tab 3: pending suggestions, accepted/rejected history, simple stats | P1 |
| FR1.5 | Each Tab 2 custom answer (FR5) shows "Auto-apply will use: *value* — source: user / learned / imported, confidence" and a lock toggle. Profile fields (FR2-FR4) display their configured values without badges | P0 |
| FR1.6 | Saving Tab 1 triggers the existing debounced rematch and redirects to recommendations. **Corrected (Phase 2):** saving Tab 2 never triggers a rematch and does not redirect. Matching reads only titles, tags, locations, excluded employers, minimum salary, remote preference and `is_active` (verified); the post-save signal now skips any save whose `update_fields` touch none of them | P0 |

### FR2: Work Authorization by Country

| ID | Requirement | Priority |
|---|---|---|
| FR2.1 | Repeater rows: country (ISO alpha-3, from `apps.web.regions`) + status + remove | P0 |
| FR2.2 | Status vocabulary is exactly `Profile.VisaStatus`: `citizen`, `permanent_resident`, `work_permit`, `requires_sponsorship`, `not_authorized`, `h1b`, `opt`, `o1`, `tn`, `e3`, `other`. No new values in this effort | P0 |
| FR2.3 | Stored in existing `Profile.visa_status_by_country` (`{alpha3: status}`) | P0 |
| FR2.4 | Unrecognized country or status is a form error (no silent drop); duplicate country is a form error | P0 |
| FR2.5 | Draft mapping stays in `_AUTHORIZATION_BY_VISA_STATUS`; statuses whose sponsorship is deliberately unknown (`h1b`, `opt`, `o1`, `tn`, `e3`, `other`) keep leaving the sponsorship field blank + `needs_review` | P0 |
| FR2.6 | T0: Importer and Learning Loop may only *propose* these values (`ProfileSuggestion` / prefilled draft value flagged `needs_confirmation`); `Profile.visa_status_by_country` is written only by a user action (form save or explicit confirm) | P0 |

### FR3: Citizenship (Passports Held)

| ID | Requirement | Priority |
|---|---|---|
| FR3.1 | Multi-select of countries (alpha-3), labeled "Countries whose passports you hold", with help text explaining it answers "Are you a citizen of X?" | P0 |
| FR3.2 | Stored in existing `Profile.citizenship_countries` | P0 |
| FR3.3 | Citizenship of a country implies `citizen` authorization for it (existing `authorization_for_country` behaviour) | P0 |
| FR3.4 | T0: Importer/Learning may only propose; `Profile.citizenship_countries` is written only by a user action | P0 |

### FR4: Salary by Region

| ID | Requirement | Priority |
|---|---|---|
| FR4.1 | One band dropdown per region key in `apps.web.regions.REGION_KEYS` (US, CA, UK, AU, IN, SG, EU), choices from `apps.web.salary_bands.SALARY_BANDS_BY_REGION` | P0 |
| FR4.2 | Stored in existing `Profile.salary_by_region` (`{region_key: band}`) — no new columns | P0 |
| FR4.3 | Resolution for a job: job country → region (`COUNTRY_ALPHA3_TO_REGION`) → that region's band. If unset, leave the salary field blank + `needs_review`. **No currency conversion of `min_salary` into an answer** (FX-converted numbers are a confident guess) | P0 |
| FR4.4 | `min_salary` + `preferred_currency` remain matching criteria (Tab 1), not auto-apply answers | P0 |
| FR4.5 | T1: Learning Loop may suggest a band (e.g. user typed the same band in review 3×); a suggested band is prefilled as `needs_confirmation` and never submitted until the user confirms | P0 |

### FR5: Custom Answers

| ID | Requirement | Priority |
|---|---|---|
| FR5.1 | Custom answers are `AnswerBank` rows (FR7), not a JSON list on Profile | P0 |
| FR5.2 | Categories: `experience`, `technologies`, `relocation`, `projects`, `education`, `availability`, `compliance`, `other` | P0 |
| FR5.3 | UI: collapsible sections per category; inline add / edit / delete / lock | P0 |
| FR5.4 | Matching at draft time, in order: (1) exact match on normalized question key (lowercased, whitespace/punctuation-collapsed, decoration such as `*`/"(optional)"/"please" and the employer's own name removed; **amended (Phase 2): the option set is no longer part of the key** — the stored value is mapped onto each form's live options at resolve time, exact or an unambiguous yes/no, otherwise blank for review); (2) otherwise embedding retrieval proposes candidates only — the field is left blank + `needs_review` with the candidate shown to the reviewer | P0 |
| FR5.5 | Select/boolean answers are mapped to the live form's option set; no option match → blank + `needs_review` | P0 |
| FR5.6 | Each row carries its risk tier (computed by the classifier, overridable to a *higher* tier by the user) | P0 |

### FR6: Application Questions Panel + Quick-Fill

| ID | Requirement | Priority |
|---|---|---|
| FR6.1 | Accordion by category listing unique normalized questions from the user's last 200 drafts | P0 |
| FR6.2 | Row: question (truncated), occurrence count, last seen, answered ✓ / unanswered, quick-fill | P0 |
| FR6.3 | Quick-fill opens a confirm modal proposing a target ("Map to Salary (US)? [Accept] [Choose field]"), displaying the proposed answer prefilled from the draft, with an editable input for both mapping and value | P0 |
| FR6.4 | **Amended (Phase 2):** quick-fill writes custom answers (FR5) only. Questions answered by typed settings (work authorization/sponsorship, citizenship, salary, contact/location) show "answered by your settings" or "not covered (reason)" and link to that section; a draft value is never pushed into a typed T0/T1 field from the panel | P0 |
| FR6.5 | Accepting writes a user-sourced, locked answer and marks the question ✓. For T0/T1 targets the prefilled value is shown as unconfirmed and needs an explicit edit/confirm action; `source_detail` records the draft origin. A value whose draft provenance was non-user is never locked without that distinct confirmation. The modal has a plain-page fallback, shows a conflict notice when the target is already set/locked, and the panel cache is invalidated on quick-fill and answer save (not only on new draft) | P0 |
| FR6.6 | Panel data cached per user for 1 hour, invalidated when a new draft is created | P0 |

### FR7: AnswerBank & Provenance (Resolver Layer)

| ID | Requirement | Priority |
|---|---|---|
| FR7.1 | `AnswerBank` model in `apps.accounts`: `profile` FK, `question_key`, `question_text`, `value` (JSON), `category`, `risk_tier`, `source` (`user` / `learned` / `imported`), `source_detail` (e.g. `resume_llm`, `github`, draft IDs), `confidence` (0–1), `is_locked`, `created_at`, `updated_at`, `expires_at` (nullable) | P0 |
| FR7.2 | One active row per `(profile, question_key)` enforced with `UniqueConstraint`; superseded values kept in an `AnswerBankHistory` table for audit | P0 |
| FR7.3 | Precedence: **user-locked > user-set > learned > imported**. A lower-precedence writer can never overwrite a higher one; it may only create a `ProfileSuggestion` | P0 |
| FR7.4 | `resolve_answer(profile, question, job=None)` in `apps/accounts/services/answer_resolver.py` returns `(value, provenance, needs_confirmation)` or `None`. Typed Profile facts (FR2–FR4, FR1.7) are consulted first, AnswerBank rows second (location-sensitive questions use only a row saved for the job's region or marked "applies everywhere"), the legacy answers last (**amended: read from backfilled `legacy:*` AnswerBank rows, not the `ExplicitAnswer` table; consulted only when no typed fact covers the question; each hit emits a fallback metric**). `needs_confirmation` is true for any T0/T1 value whose source is not a user-confirmed row | P0 |
| FR7.5 | `apps/auto_apply/services/answer_resolution.py:resolve_field_answers` (the real `ExplicitAnswer` read path; `drafting.py` only calls it) must obtain answers via `resolve_answer()`, ahead of the LLM batch step. `resolve_answer()` replaces `_profile_derived_answer` and the category map there | P0 |
| FR7.8 | **Tier enforced at resolve time:** `resolve_answer()` re-classifies the question text and uses the higher of stored and computed tier, so classifier fixes take effect on existing rows. A DB `CheckConstraint` forbids `source in (learned, imported)` rows from being marked confirmed/locked | P0 |
| FR7.10 | **`Profile.field_provenance`** (JSON, `{field_name: {source, locked, updated_at, detail}}`) gives plain Profile columns the same provenance as AnswerBank rows. Covered fields: `full_name`, `phone`, `current_employer`, `linkedin_url`, `github_url`, `portfolio_url`, `target_tags`, the FR1.7 contact/location facts, and per-key entries for `visa_status_by_country` / `citizenship_countries` / `salary_by_region` (`visa_status_by_country.USA`). A form save marks every field the user changed as `source=user` (and `locked` when the user toggles it); importer/learner writes record `imported`/`learned`. A missing entry means `user` for any non-empty legacy value. FR7.3 precedence applies: a lower-precedence writer cannot overwrite a higher one or a locked field — it may only create a diff row/`ProfileSuggestion`. One shared `set_profile_field(profile, field, value, source)` helper is the only write path for covered fields, so no writer can skip the check | P0 |
| FR7.9 | Submit gate: a draft containing any `needs_confirmation` T0/T1 field cannot be submitted until each is confirmed or edited in the review queue; the confirmed value and its prior provenance are recorded in the FR7.6 snapshot | P0 |
| FR7.6 | Submitted drafts snapshot the exact answers + provenance used (legal-attestation audit trail) | P0 |
| FR7.7 | Expiry: `learned` T2 answers expire after 180 days, `imported` after 365 days unless re-confirmed; expired rows resolve to `None` | P0 |

### FR8: Learning Loop

| ID | Requirement | Priority |
|---|---|---|
| FR8.1 | Signals: review-queue edits (primary), user-filled `needs_review` fields, application outcomes, quick-fill accepts | P1 |
| FR8.2 | **Exact-match learner**: when the user gives the same answer to the same normalized question key on ≥3 drafts, propose an AnswerBank entry | P1 |
| FR8.3 | **Embedding retrieval** (pgvector, 384-dim `all-MiniLM-L6-v2`): proposes candidate answers for unseen questions; never writes directly | P1 |
| FR8.4 | Tier gating: all tiers are learnable. T0/T1 → suggestion and `needs_confirmation` prefill only, never submitted unreviewed (FR7.9); T2 → suggestion, or auto-apply only after the FR8.6 gate. An accepted suggestion becomes `source=user` only through an explicit confirm; accepting a T1 suggestion does not make it auto-apply without per-question confirmation being stored (locked user row) | P1 |
| FR8.5 | Execution contexts: (1) incremental after each draft outcome, (2) nightly batch 03:00 (Beat entry, `env.int` tunables), (3) on-demand "Improve my profile". All three are idempotent and keyed on `(profile, question_key)` | P1 |
| FR8.6 | **Shadow mode**: for ≥2 weeks log what the learner *would* have filled vs. what the user actually entered. The unit of observation is **(user, job, question)**: one record per draft question, correct if the shadow value equals what the user finally submitted. Observations roll up first to per-(user, `question_key`), then to a global per-`question_key` figure once enough distinct users have data. Enable T2 auto-apply for a scope only if precision ≥99% (lower confidence bound, minimum n≥30 observations) for 2 consecutive weeks; a scope with too few observations stays suggest-only. Re-evaluated weekly, auto-disabled on regression. Silently accepted drafts (no edit) count as correct | P1 |
| FR8.7 | `ProfileSuggestion` model: profile, question_key, proposed value, evidence (draft IDs), confidence, status (`pending` / `accepted` / `rejected` / `expired`), timestamps. Unresolved suggestions expire after 90 days | P1 |
| FR8.8 | `Profile.learning_enabled` (default True) disables all learning writes and suggestions | P1 |
| FR8.9 | Application `FAILED` outcomes are used only for gap detection (unanswered questions), never as negative evidence about answer correctness | P1 |
| FR8.10 | Embedding model runs only in the worker image; web image does not import torch | P1 |

### FR9: Profile Import (Resume / LinkedIn PDF / GitHub)

| ID | Requirement | Priority |
|---|---|---|
| FR9.1 | One structured-output LLM call (provider via existing LangChain multi-provider layer, default GPT-4o-mini) extracts contact, skills, education, projects, experience | P1 |
| FR9.2 | Every extracted value must be grounded in a source span of the document text; ungrounded values are dropped | P1 |
| FR9.3 | Deterministic validation: regex for email/phone/URLs/dates. Rule-based parser (`pypdf`) is the fallback when the LLM is unavailable. Must use RE2 or regex execution timeouts and input length bounds to prevent ReDoS | P1 |
| FR9.4 | Resume text is untrusted input: fixed system prompt with the document delimited as data, output schema enforced. The importer may *propose* T0/T1 values but never writes them as confirmed; they surface as `needs_confirmation`. URL fields are restricted to `https` with no `javascript:`/`data:`; the review template relies on Django autoescape (no `|safe`) | P1 |
| FR9.5 | LinkedIn: PDF export upload through the same pipeline. **No scraping** | P1 |
| FR9.6 | GitHub: public REST API by username (no OAuth): languages → `target_tags` suggestions, top repos → `portfolio_url` suggestion | P1 |
| FR9.7 | Runs async in Celery; UI polls a status endpoint for progress and redirects to the dedicated review route upon completion | P1 |
| FR9.8 | Results are shown via a dedicated review route (`/profile/import/<task_id>/review/`) displaying a two-column diff (Current vs. Imported) with checkboxes to accept/reject/edit each field before any write; accepted values are written with `source=imported` | P1 |
| FR9.9 | Re-sync (resume / LinkedIn / GitHub) shows a diff and never touches user-set or locked values, as recorded in `Profile.field_provenance` (FR7.10) and AnswerBank `source`/`is_locked`. Locked/user-set rows appear in the diff read-only with a "kept" label | P1 |
| FR9.10 | Consent: unchecked-by-default opt-in next to the upload control, stored as `Profile.llm_import_consent_at` (+ version) and checked inside the Celery task before any LLM call; absent/declined → rule-based parser. Only providers on a zero-retention allowlist may be used (fail closed). Resume text and prompts/responses are never logged | P1 |
| FR9.11 | Imported values do **not** seed the learning loop as evidence (avoid parser errors compounding into "learned" answers) | P1 |
| FR9.12 | GitHub fetch: username validated against `^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`, requests only to `https://api.github.com` with the username URL-quoted, redirects disabled, timeout and response-size caps, per-user rate limit, only whitelisted JSON fields consumed | P1 |
| FR9.13 | PDF upload: max size (default 5 MB) and page cap, `%PDF` magic-byte check, encrypted/JS-bearing PDFs rejected, Celery time/memory limits, non-public storage with randomized names served only through an owner-checked view, extracted text truncated to a fixed token budget before any LLM call | P1 |
| FR9.14 | `ImportJob` record (profile FK, task_id, status, expires_at). Every `/profile/import/<task_id>/*`, `/profile/suggestions/` and `/profile/answers/` lookup filters by `request.user.profile` (404 on mismatch); Celery results are never read by raw task_id; the diff payload is deleted after apply or TTL | P1 |

---

## Non-Functional Requirements

| ID | Requirement | Target |
|---|---|---|
| NFR1 | Auto-apply `unanswerable_required` rate | ≤12% (from ~18%) |
| NFR2 | T0/T1 answers submitted without explicit user confirmation (learned/imported/embedding-sourced) | **0** (hard invariant, covered by tests incl. a labelled corpus of real Greenhouse legal/commercial questions) |
| NFR3 | Draft → Applied conversion | +15% relative |
| NFR4 | Suggestion acceptance rate | ≥60% |
| NFR5 | T2 auto-apply precision (shadow-measured) | ≥99% before enablement |
| NFR6 | Import latency (LLM path, p95) | <10 s, async |
| NFR7 | Questions panel load (cached) | <1 s |
| NFR8 | Deletion: on account deletion everything persisted or cached for the user is deleted (AnswerBank, history, suggestions, embeddings, imports, caches). Application/form-field server data is temporary. Full GDPR export/retention design is **deferred** | Deferred |

---

## Data Model Changes

Existing (no change): `Profile.visa_status_by_country`, `citizenship_countries`,
`salary_by_region`, `preferred_currency`, `min_salary`, `github_url`,
`portfolio_url`, `current_employer`.

New (directional, conventions per AGENTS.md — `TextChoices`, `is_` booleans,
explicit `related_name`, `UniqueConstraint`):

```python
# apps/accounts/models.py (sketch, not implementation)
class Profile:
    learning_enabled = BooleanField(default=True)
    field_provenance = JSONField(default=dict)         # FR7.10
    llm_import_consent_at = DateTimeField(null=True)   # FR9.10
    llm_import_consent_version = CharField(blank=True)

class ImportJob(Model):                  # FR9.14, ownership-bound import task
    profile = FK(Profile, CASCADE, related_name="import_jobs")
    task_id, status, expires_at, created_at / updated_at

class AnswerBank(Model):
    class Source(TextChoices): USER, LEARNED, IMPORTED
    class RiskTier(TextChoices): T0_LEGAL, T1_COMMERCIAL, T2_FACTUAL
    profile = FK(Profile, CASCADE, related_name="answer_bank")
    question_key = CharField(max_length=255)
    scope_region = CharField(max_length=8, blank=True)   # "" = everywhere (Phase 2)
    question_text = TextField()
    value = JSONField()
    category = CharField(choices=...)
    risk_tier = CharField(choices=RiskTier.choices)
    source = CharField(choices=Source.choices)
    source_detail = JSONField(default=dict)
    confidence = FloatField(default=1.0)
    is_locked = BooleanField(default=False)
    expires_at = DateTimeField(null=True)
    created_at / updated_at
    # UniqueConstraint(profile, question_key, scope_region)  (Phase 2)

class AnswerBankHistory(Model): ...      # append-only superseded values (kept: remember-on-review and deletes write it)
class AnswerObservation(Model):          # Phase 2: what was actually submitted, per (user, job, question)
    profile = FK(Profile, CASCADE, related_name="answer_observations")
    question_key, question_text, value, tier, field_type, provenance_source/origin, was_edited,
    job_id, draft_id, employer_name, job_region, created_at   # append-only; input to FR8.2/FR8.6
class ProfileSuggestion(Model): ...      # FR8.7
class QuestionEmbedding(Model):          # FR8.3, pgvector
    profile = FK(Profile, CASCADE, related_name="question_embeddings")
    question_key, question_text, embedding = VectorField(384)
```

`ExplicitAnswer` (`apps/auto_apply/models.py`) is migrated, not deleted
immediately:

1. Phase 1: resolver reads Profile facts → AnswerBank → `ExplicitAnswer` (legacy fallback).
2. Phase 2: **backfill, then read-only (amended from dual write).** The page that wrote `ExplicitAnswer` is removed, so nothing writes it any more and there is nothing to keep in sync. A one-time migration (`auto_apply` 0011) copies every row into AnswerBank as a locked `source=user` `legacy:*` row holding a readable value plus the yes/no it means (the dev DB has only 3: `work_authorization`, `sponsorship`, `salary_expectation`). A salary band is bound to its single region, so it only ever answers a job quoted in that currency. The table is kept, read-only in the admin, until Phase 5. Legacy categories map to synthetic keys `legacy:work_authorization` / `legacy:sponsorship` (T0) and `legacy:salary_expectation` (T1); `OTHER` rows become custom answers. Typed Profile facts (FR2–FR4) still win in the resolver; a legacy row that disagrees with a typed fact is logged and surfaced in the review queue, not silently shadowed.
3. Phase 5: after one release with no fallback hits (logged metric), stop dual write, drop `ExplicitAnswer` and its page.

---

## Endpoints

All `/profile/*` endpoints must be authenticated and explicitly authorize access to ensure users can only read or write data, answers, and import tasks linked to their own profile.

| Endpoint | Method | Purpose |
|---|---|---|
| `/profile/` | GET/POST | Tabbed profile (tab-scoped submit) |
| `/profile/answers/` | POST | Create/update/lock AnswerBank rows |
| `/profile/questions-panel/` | GET | Cached panel data |
| `/profile/suggestions/` | GET/POST | List / accept / reject suggestions |
| `/profile/learn/` | POST | On-demand learning run |
| `/profile/import/` | POST | Start resume / LinkedIn PDF / GitHub import → task id |
| `/profile/import/<task_id>/` | GET | Import status + proposed field diff |

`resolve_answer()` is an internal service, not an HTTP endpoint.

---

## Implementation Phases

| Phase | Weeks | Scope | Exit criterion |
|---|---|---|---|
| **1. Foundation** | 1–2 | AnswerBank + history, risk classifier, `resolve_answer()` with ExplicitAnswer fallback, drafting switched to resolver, answer snapshot on submit | Drafting tests green; no behaviour change for existing users |
| **2. Profile UI** | 3–4 | Two-page profile, typed-fact resolver (FR2–FR4, FR1.7), custom answers CRUD, remember-on-review + observation capture, questions panel + quick-fill, ExplicitAnswer backfill | Users can maintain all answers from one page; a question answered once is not re-entered |
| **3. Learning (consensus)** | 5 | Consensus learner over `AnswerObservation` (same answer across 2+ distinct jobs → learned row; T0/T1 confirm-each-time), suggestions/Learning tab, "confirm all remembered answers", shadow metrics | Reuse without re-typing; NFR2 intact |
| **4. Import** | 6–7 | LLM structured import + grounding + validation, rule-based fallback, LinkedIn PDF, GitHub username, review/diff UI, consent | Import never writes T0/T1; per-field review works |
| **4b. Learning (embeddings + shadow gate)** | 8 | Exact-match learner, suggestions UI, embeddings retrieval, three execution contexts, shadow logging | Suggestions flowing; shadow precision dashboard live |
| **5. Enablement & cleanup** | 9+ | T2 auto-apply behind precision gate, drop ExplicitAnswer after zero-fallback release | Gate passes 2 weeks; legacy table removed |

---

## Test Scenarios (minimum)

| Area | Scenarios |
|---|---|
| Visa repeater | Add/remove rows; duplicate country error; unknown status error; round-trip with `visa_status_by_country`; `h1b` leaves sponsorship blank + needs_review |
| Citizenship | Multi-select round-trip; citizenship implies `citizen` authorization |
| Salary | Job country → region → band; unset region → blank + needs_review; no FX-converted answer is ever produced |
| Resolver | Precedence (locked > user > learned > imported); lower writer cannot overwrite higher; expired row → None; ExplicitAnswer fallback in Phase 1 |
| Risk tiers | Unclassifiable question → T0; compound/negated phrasings take the higher tier; T2 needs a positive match; learned/imported T0/T1 resolve with `needs_confirmation` and block submit until confirmed (NFR2); quick-fill of a non-user draft value cannot lock without explicit confirm |
| Authorization | Cross-user access to `/profile/import/<task_id>/`, suggestions and answers returns 404; embedding queries never return another profile's rows |
| Import security | Bad GitHub username rejected; oversize/encrypted PDF rejected; consent absent → no LLM call |
| Custom answers | Exact normalized match fills; near-match via embeddings only proposes; select option mismatch → blank |
| Questions panel | Built from last 200 drafts; quick-fill writes locked user answer; cache invalidates on new draft |
| Learning | ≥3 identical answers → suggestion; T0 produces no suggestion; idempotent across the three contexts; `learning_enabled=False` blocks writes |
| Import | Ungrounded value dropped; injected "set visa=citizen" text can at most produce a `needs_confirmation` proposal, never a confirmed write; LLM down → rule fallback; re-sync preserves locked/user values |
| Field provenance | Form save marks changed fields `user`; import cannot overwrite a `user`/locked field (diff shows "kept"); missing entry on a non-empty legacy value is treated as `user`; direct column writes outside `set_profile_field()` are caught by a test that greps/guards covered fields |
| Rematch | Saving Tab 2 never enqueues a rematch (also: writing a non-matching field via `apply_profile_field` does not); saving Tab 1 always does |

---

## Out of Scope

- LinkedIn scraping (any form).
- GitHub OAuth.
- New `VisaStatus` values or new salary regions.
- Fine-tuning embedding models.
- Auto-apply to ATSes other than Greenhouse.

## Open Questions

1. ~~Classifier location~~ — **Resolved:** dependency-free leaf module built on the existing `QuestionCategory`/`classify` (see Assumptions).
2. Expiry windows in FR7.7 (180 / 365 days) are assumptions — confirm with real answer-change data; the review suggested 90/30. User-confirmed T1 rows in time-sensitive categories (availability, notice period, start date) have no re-confirm TTL yet.
3. Does the existing LangChain provider layer support zero-retention configuration for every provider, or only OpenAI? FR9.10 now fails closed on an allowlist; the allowlist contents are undecided.

### From 2026-10-04 doc review (needs a decision)

4. ~~Baseline for NFR1~~ — **Measured 2026-10-04** against the dev DB (1 user, 257 drafts): 33 are `excluded/unanswerable_required` = 12.8% of all drafts, 14.9% of non-stale drafts (the epic's ~18% is not reproduced). Of those 33: 13 include a work-auth/sponsorship question (T0), 7 salary (T1), 27 involve phone/city/country/address/timezone, 8 involve how-did-you-hear/referral/links/experience, and 17 contain no T0/T1 question at all. So T2 and contact facts are the larger addressable share; see FR1.7. Re-baseline NFR1 (target ≤12% is already nearly met on this sample) once real multi-user data exists.
5. ~~Priority cut line~~ — **Decided:** Phases 1–2 (resolver, tiers, tabbed Profile/Answers UI, FR2–FR7, FR1.7) are P0; learning (FR8) and import (FR9) are P1; Learning tab appears with Phase 4.
6. ~~Embeddings and import timing~~ — **Follows from 5:** both are P1 and ship after Phases 1–2 are measured. Embedding/worker-image work (FR8.3/FR8.10) stays inside Phase 4 and can be dropped if exact-match coverage is enough.
7. ~~Shadow gate unit~~ — **Decided:** observe per (user, job, question), roll up per (user, question_key), then to a global per-question_key figure when enough distinct users exist (FR8.6). Still open: what the learning loop delivers if no scope ever reaches the 99% gate (suggestions accepted into AnswerBank still count toward the metric).
8. ~~Imported provenance on Profile columns~~ — **Decided:** `Profile.field_provenance` JSON (FR7.10), written only through one `set_profile_field()` helper. Open detail: backfill treats existing non-empty values as `user`.
9. ~~ExplicitAnswer migration mapping~~ — **Decided: dual write** (see Data Model Changes). Open detail: behavior when a legacy row disagrees with a typed Profile fact (currently: logged + surfaced in review queue).
10. ~~`AnswerBankHistory`~~ — **Kept (Phase 2):** remember-on-review, overwrite and delete all write it, so it has consumers.
11. ~~GDPR scope~~ — **Deferred** (NFR8). Policy for now: on account deletion delete everything persisted/cached; application/form-field server data is temporary. Still make snapshots/history write-once and admin read-only.
12. **Timeline:** with learning/import at P1, Phases 1–2 are the committed scope; enablement (shadow ≥2 weeks + gate ≥2 weeks after Phase 4) lands around week 11+. Epic says 9 weeks.
13. **Epic #117 vs this doc:** epic lists 9 categories incl. `visa_details`, 10 visa statuses, global salary fallback, WebSocket progress, pdfplumber; doc has 8, 11 (matches code), none, polling, pypdf. Update the epic text; #113/#112/#100/#16 are still open despite "supersedes".
14. **UI specs missing:** tab URLs/submit boundaries, import progress/diff/failure states, Learning-tab suggestion cards and empty states, provenance badge/lock semantics, accessibility/responsive NFR, FR1.6 redirect vs import flow.
15. **Rate limits/CSRF:** per-user quotas for `/profile/import/` and `/profile/learn/`; state CSRF coverage for new POST endpoints.

## Sources

- `2026-10-03-1430-profile-overhaul-unified-answers-plan.md`
- `2026-10-03-1530-self-learning-profile-loop-plan.md`
- `2026-10-03-2000` … `2026-10-03-2110` decision matrices
- `2026-10-03-2200-synthesized-review.md` (Claude + AGY review)
- Code: `apps/accounts/models.py` (`VisaStatus`, `_AUTHORIZATION_BY_VISA_STATUS`, `salary_by_region`), `apps/web/regions.py`, `apps/web/salary_bands.py`, `apps/auto_apply/models.py` (`ExplicitAnswer`)


## Architecture & Decisions
- **`apps.accounts.models.AnswerBank` and `AnswerBankHistory`**: Centralizes all answers. It includes provenance (`user`, `learned`, `imported`) and risk tiering.
- **`apps.accounts.services.answer_resolver.resolve_answer`**: Single entrypoint for drafting to get an answer. It reads Profile facts first, then AnswerBank, then ExplicitAnswer (as fallback for phase 1).
- **`apps.auto_apply.models.ExplicitAnswer`**: Maintained for Phase 1 as a fallback. Will be migrated and deprecated.
- **Learning Loop**: Uses exact match first. If not found, pgvector is used to retrieve candidate answers. Candidates are proposed as `ProfileSuggestion`.
- **Import Pipeline**: Asynchronous celery task `parse_resume` extracts structured data; T0/T1 values come out only as `needs_confirmation` proposals. It requires user review before applying.

## Assumptions
- Expiry windows of 180 (learned) and 365 (imported) days are acceptable starting points.
- Langchain supports zero-retention parameters for the chosen provider (OpenAI is default and supports it).
- The tier classifier is a dependency-free leaf module (e.g. `apps/answers_core/tiering.py`, name TBD) that wraps/extends `apps/auto_apply/llm/categories.py` (`QuestionCategory`, `classify`), imported by both `accounts` and `auto_apply`, so `accounts` never imports `auto_apply`. The resolver's legacy `ExplicitAnswer` fallback is injected by `auto_apply` rather than imported by `accounts`.

## Implementation Units

### Unit 1: Foundation (AnswerBank & Resolver)
**Objective**: Build AnswerBank, History, and `resolve_answer` with fallback. Update drafting.
- `apps/accounts/models.py`: Add `AnswerBank`, `AnswerBankHistory`. Add `UniqueConstraint(profile, question_key)`.
- Tier classifier leaf module (see Assumptions): T0/T1/T2 mapping over the existing `QuestionCategory`/`classify`; positive-match T2; higher-tier-wins. Add the labelled Greenhouse question corpus as a test fixture.
- `apps/accounts/services/__init__.py` and `apps/accounts/services/answer_resolver.py`: `resolve_answer(profile, question, job=None)` returning `(value, provenance, needs_confirmation)`; resolve-time re-classification (FR7.8); legacy `ExplicitAnswer` fallback injected from `auto_apply`, emitting a fallback-hit metric.
- `apps/auto_apply/services/answer_resolution.py` (`resolve_field_answers`): call `resolve_answer()` ahead of the LLM batch step; replace `_profile_derived_answer` and the category map. `drafting.py` only needs to carry the `needs_confirmation` flag through to the review queue and the submit gate (FR7.9).
- `AnswerBank` `CheckConstraint` (FR7.8); append-only `AnswerBankHistory`/snapshot models read-only in admin.
- `Profile.field_provenance` + `set_profile_field()` helper (FR7.10); route the existing Profile form save through it and backfill (non-empty values → `user`).
- **No T2 auto-apply gate logic in this unit** (moved to Units 4–5). Until the gate exists, learned T2 values are suggest-only.
- **Test File**: `apps/accounts/tests/test_answer_resolver.py`

### Unit 2: Profile UI Overhaul
**Objective**: Build the tabbed Profile UI and custom answers CRUD.
- Module layout: `apps/web/views.py` and `forms.py` are single files; use flat sibling modules (`views_profile.py`, `forms_profile.py`, `views_suggestions.py`, `views_profile_import.py`) rather than packages that would shadow them. The path names below stand for those modules.
- `apps/web/views/profile.py`: Implement tabs: Profile, Auto-apply Answers, Learning. Add endpoints `/profile/`, `/profile/answers/`, and `/profile/questions-panel/`. Cache invalidation on quick-fill/answer save (FR6.5/6.6); Tab 2 rematch only if FR2/FR3 changed (FR1.6).
- Migration mapping for `ExplicitAnswer` → `AnswerBank` is specified before this unit starts (Open Question 9); the migration lives in `auto_apply` with a dependency on the new accounts migration.
- `apps/accounts/migrations/00XX_migrate_explicit_answers.py`: Data migration copying `ExplicitAnswer` to `AnswerBank` (`source=user`, `is_locked=True`).
- `apps/web/templates/profile/`: Create templates for the three tabs.
- `apps/web/forms/profile.py`: Implement forms for Visa repeater, citizenship multi-select, and salary band by region.
- `apps/accounts/services/questions_panel.py`: Implement cached last 200 questions logic.
- **Test File**: `apps/web/tests/test_profile_views.py`

### Unit 3: Import Pipeline
**Objective**: Extract data from Resume/LinkedIn PDF via LLM with rule fallback. Extract target tags and repos from GitHub API.
- `apps/accounts/tasks.py`: Extend `parse_resume` to use LangChain structured output. Implement rule-based fallback with `pypdf`.
- `apps/accounts/services/importer.py`: Orchestrate grounding and validation. Ensure T0/T1 fields are never written. Implement GitHub REST API client for tags and repos.
- `apps/web/views/profile_import.py`: Endpoints `/profile/import/`, `/profile/import/<task_id>/` and `/profile/import/<task_id>/review/` (progress + two-column diff, template included). `ImportJob` ownership binding (FR9.14).
- Consent gate and persisted `llm_import_consent_at` checked in the task (FR9.10); GitHub validation/egress limits (FR9.12); PDF limits and storage (FR9.13). Pick one ReDoS mitigation (e.g. pinned `google-re2` in `requirements/base.txt`) and reuse the existing `extract_text_from_pdf`; define how source spans are tracked for FR9.2 grounding.
- **Test File**: `apps/accounts/tests/test_importer.py`

### Unit 4: Learning Loop
- `requirements/worker.txt` (base + CPU-only torch + `sentence-transformers`) passed via the existing `REQUIREMENTS` build arg for the `worker` service only in `docker-compose.yml`; no second Dockerfile. Bake the pinned MiniLM weights (checksum, no runtime download) into the image. Embeddings are computed only in a worker-queue Celery task; web reads precomputed `QuestionEmbedding` rows. All similarity queries go through one service function that filters by profile first.
- Shadow logging, weekly precision evaluation, T2 gate and auto-disable live here (not Unit 1).
**Objective**: Exact-match learner, embeddings retrieval, and suggestion pipeline.
- `apps/accounts/models.py`: Add `ProfileSuggestion` and `QuestionEmbedding`. Add `learning_enabled = BooleanField(default=True)` to `Profile`.
- `apps/accounts/services/learning.py`: Implement exact-match learner (≥3 identical answers) and embedding retrieval.
- `apps/accounts/tasks.py`: Add `incremental_learning_loop`, `nightly_learning_batch`, shadow mode logging, and weekly precision evaluation task. Add on-demand Celery task trigger.
- `apps/web/views/suggestions.py`: Add endpoints for `/profile/suggestions/` and `/profile/learn/`.
- **Test File**: `apps/accounts/tests/test_learning_loop.py`

### Unit 5: Enablement & Cleanup
**Objective**: Enable T2 auto-apply behind the gate; clean up ExplicitAnswer post-migration.
- T2 enablement behind the weekly precision evaluation (default-off flag), with auto-disable on regression (FR8.6).
- GDPR export/delete covering every profile-linked store (NFR8, Open Question 11), with a test enumerating all models with a profile FK.
- After a release with zero fallback hits: remove `ExplicitAnswersForm`, its view/URL, admin registration and tests, and add the drop migration for `ExplicitAnswer`.
- **Test File**: `apps/auto_apply/tests/test_explicit_answer_phaseout.py`
