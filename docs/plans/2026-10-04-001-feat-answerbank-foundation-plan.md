---
title: "feat: AnswerBank foundation (Phase 1 of Profile Overhaul, epic #117)"
type: feat
status: completed
origin: docs/plans/2026-10-03-2300-consolidated-requirements.md
depth: deep
---

# feat: AnswerBank foundation (Phase 1, epic #117)

## Context

"First issue" is read as Phase 1 (Foundation) of epic #117, the first phase in its implementation order. The requirements doc (committed `91c2eb5`) defines it as: AnswerBank + history, tier classifier, `resolve_answer()` with legacy ExplicitAnswer fallback, drafting switched to the resolver, answer snapshot on submit, plus `Profile.field_provenance`. Exit criterion: drafting tests green and **no behaviour change for existing users**.

Phase 1 must be inert for current users: no learned/imported sources exist yet, so the new `needs_confirmation` gate is built and tested but dormant. Contact/location columns (FR1.7), UI tabs, dual write and the migration backfill are Phase 2 and are out of scope here.

## Scope boundaries

In scope: tiering leaf module, `AnswerBank`/`AnswerBankHistory`, `Profile.field_provenance` + write helper, `resolve_answer()`, wiring into `answer_resolution.py`, confirmation gate + submit snapshot.

### Deferred to follow-up work (Phase 2+)
- Tabbed UI, FR1.7 contact/location columns, ExplicitAnswer dual write/backfill, questions panel.
- Folding the post-resolution salary-by-region override (`apps/auto_apply/services/drafting.py`, ~331-367) into the resolver (FR4.3). Phase 1 leaves it where it is.
- Pre-existing bugs found while exploring, to be filed as issues: `ExplicitAnswersForm` visa maps in `apps/web/forms.py` duplicate `_AUTHORIZATION_BY_VISA_STATUS` and disagree on sponsorship (`yes_h1b` vs `""`); `explicit_answers` view discards the bound form on an invalid POST (`apps/web/views.py`).
- `ExplicitAnswer.answer_text` stores choice keys (`yes_h1b`), not display values; migration mapping belongs to Phase 2.

## Key technical decisions

- **Dependency direction.** `apps/accounts` must not import `apps/auto_apply` (auto_apply already imports accounts). The tier classifier becomes a dependency-free module `apps/accounts/tiering.py` (mirrors the `apps/locations/engine.py` leaf: pure, no DB). `QuestionCategory`/`classify` move there; `apps/auto_apply/llm/categories.py` re-exports them so the two existing callers and tests keep working. The resolver lives in `apps/accounts/services/answer_resolver.py` and receives the legacy ExplicitAnswer lookup as an **injected callable** from auto_apply.
- **Tier derivation.** T0 = WORK_AUTHORIZATION, LEGAL_ATTESTATION, BACKGROUND_CHECK, DEMOGRAPHIC (existing hard-excluded categories); T1 = SALARY_EXPECTATION plus new patterns (notice period, start date, relocation); T2 requires a positive allowlist match; any question also matching a T0/T1 pattern takes the higher tier; unclassifiable defaults to T0. Resolve-time tier = max(stored, computed).
- **Reuse the existing review gate.** The only "blocked until reviewed" check today is in `send_auto_apply_draft` (`apps/web/views.py` ~717-723) keyed on `entry["needs_review"]`; `submit_auto_apply_draft` (`apps/auto_apply/tasks.py` ~255-396) has no check. Add `needs_confirmation` to the answers JSON entry shape and enforce it in **both** the view and the task (defence in depth). "Reviewed" is `user_confirmed`, set only by `edit_auto_apply_draft`; that view currently confirms every non-blank posted field implicitly, so `needs_confirmation` fields need an explicit per-field confirm control.
- **Snapshot (FR7.6).** New `AutoApplyDraft.submitted_answers_snapshot` JSONField, written in the same atomic `UPDATE` that moves the draft to SENDING. Do not repurpose `answers_schema_version` (unused, default 1) except to bump it to 2 for the new entry shape.
- **Provenance helper pattern.** Model on `Profile.set_resume` / `EmailInboxCredential` mutators (explicit `update_fields`). Note `apps/matching/signals.py` triggers a rematch on every Profile `post_save`, so provenance bookkeeping must ride the same save as the field change, never add a second save.
- **Constraints.** No `CheckConstraint` exists in the repo yet; this will be the first. Name UniqueConstraints `uniq_<model>_<fields>`.

## Implementation units

### U1. Tiering leaf module
- **Goal:** pure T0/T1/T2 classification built on existing categories.
- **Requirements:** FR7.8, risk tiers, Open Question 1 (closed).
- **Files:** `apps/accounts/tiering.py` (new); `apps/auto_apply/llm/categories.py` (re-export); `apps/accounts/tests/test_tiering.py`; `apps/accounts/tests/fixtures/tier_corpus.json` (labelled real Greenhouse questions, including compound/negated phrasings; seed from distinct labels in `auto_apply_autoapplydraft.exclusion_reason` and `form_schema_snapshot`).
- **Patterns:** `apps/locations/engine.py` (no DB/network, no imports of other apps); existing `apps/auto_apply/tests/test_question_categories.py`.
- **Test scenarios:** each existing category keeps its current classification (re-run `test_question_categories.py` unchanged); compound "years of experience and are you authorized to work" → T0; unclassifiable → T0; T2 only on positive match; sponsorship/visa/EEO variants → T0; salary/notice/start-date/relocation → T1; corpus recall for T0/T1 = 100%.
- **Verification:** `apps.auto_apply` category tests pass untouched; module imports nothing from Django ORM or other apps.

### U2. AnswerBank models and Profile.field_provenance
- **Goal:** schema for FR7.1-7.3, FR7.10.
- **Dependencies:** U1 (tier enum).
- **Files:** `apps/accounts/models.py`; `apps/accounts/migrations/0010_answerbank_field_provenance.py`; `apps/accounts/admin.py` (history read-only); `apps/accounts/tests/test_answerbank_models.py`.
- **Approach:** `AnswerBank` with `Source`/`RiskTier` TextChoices, `UniqueConstraint(profile, question_key)`, `CheckConstraint` forbidding `source in (learned, imported)` with `is_locked=True`, `expires_at`; `AnswerBankHistory` append-only (no admin change/delete); `Profile.field_provenance` JSONField(default=dict) with the shape documented above the field. Migration is schema-only (no backfill; Phase 2).
- **Test scenarios:** duplicate `(profile, question_key)` rejected; learned+locked rejected by DB; history rows cannot be edited via admin; `field_provenance` defaults to `{}`; migration applies on a DB with existing profiles.
- **Verification:** `makemigrations --check` clean; existing accounts tests pass.

### U3. Profile field provenance write path
- **Goal:** one place that records who wrote a covered Profile field and refuses lower-precedence overwrites.
- **Dependencies:** U2.
- **Files:** `apps/accounts/models.py` (method on `Profile`) or `apps/accounts/services/profile_fields.py`; `apps/web/forms.py` (`ProfileForm.save`, ~207-229); `apps/accounts/admin.py` (`save_model`, ~172-192); `apps/accounts/tasks.py` (`parse_resume`); `apps/accounts/tests/test_field_provenance.py`; `apps/web/tests/test_auth_and_profile.py`.
- **Approach:** covered fields per FR7.10 (`full_name`, `phone`, `current_employer`, the three URLs, `target_tags`, and per-key `visa_status_by_country.<ISO>` / `citizenship_countries` / `salary_by_region`). `ProfileForm.save` diffs initial vs cleaned data and stamps `source=user` in the **same** save. Missing entry on a non-empty value is treated as `user`. Importer/learner paths (Phase 3+) must call the helper; add a test that fails if a covered field is written elsewhere is deferred to Phase 3 when those writers exist.
- **Test scenarios:** form save marks only changed fields; unchanged fields untouched; locked/user field not overwritten by `source=imported` call (returns "kept"); legacy non-empty value with no entry resolves as `user`; one `post_save` per form save (no extra rematch).
- **Verification:** existing profile form tests green; rematch scheduling count unchanged.

### U4. `resolve_answer()` service
- **Goal:** FR7.3/7.4/7.7/7.8 as a pure-ish service.
- **Dependencies:** U1, U2.
- **Files:** `apps/accounts/services/__init__.py` (new package); `apps/accounts/services/answer_resolver.py`; `apps/accounts/tests/test_answer_resolver.py`.
- **Approach:** `resolve_answer(profile, question, job=None, *, legacy_lookup=None) -> ResolvedValue | None` returning `(value, provenance, needs_confirmation)`. Order: typed Profile facts (`authorization_for_country`, `salary_by_region`) → AnswerBank → injected legacy lookup. Re-classify at resolve time and take the higher tier; expired rows resolve to `None` (180/365 days for learned/imported T2 rows); `needs_confirmation` true for any T0/T1 whose source is not a user-confirmed row. Emits a logged fallback-hit counter on legacy use.
- **Test scenarios:** precedence locked > user > learned > imported; lower writer cannot overwrite; expired row → None; stored tier T2 but computed T0 → T0 wins; learned T1 row → needs_confirmation; legacy fallback used only when AnswerBank and typed facts miss, and logs a hit; no import from `apps.auto_apply` (assert via import test).
- **Verification:** resolver tests pass with no auto_apply import in `apps/accounts`.

### U5. Wire the resolver into answer resolution (behaviour-preserving)
- **Goal:** FR7.5 with zero behaviour change.
- **Dependencies:** U4.
- **Files:** `apps/auto_apply/services/answer_resolution.py` (`resolve_field_answers`, `_explicit_categories_for`, `_profile_derived_answer`); `apps/auto_apply/tests/test_answer_resolution.py`; `apps/auto_apply/tests/test_drafting_service.py`.
- **Approach:** replace the direct `ExplicitAnswer` query and `_profile_derived_answer` with `resolve_answer(..., legacy_lookup=<closure over ExplicitAnswer incl. the sponsorship/authorization split logic>)`. Keep `_enforce_option_constraint`, the LLM batch call, and the order of results. Carry the new `needs_confirmation` flag through `ResolvedAnswer` (new optional field, default False) and into the `answers` entry in `drafting.py` (leave the salary-region override untouched).
- **Execution note:** characterization-first. Run the existing `test_answer_resolution.py` and `test_drafting_service.py` unchanged before and after; they are the no-behaviour-change proof.
- **Test scenarios:** all existing tests pass unmodified; an AnswerBank user-locked row now resolves ahead of legacy; a seeded learned T0 row yields `needs_review=True, needs_confirmation=True` and never reaches the LLM.
- **Verification:** full `apps.auto_apply` suite green.

### U6. Confirmation gate and submit snapshot
- **Goal:** FR7.6, FR7.9.
- **Dependencies:** U5.
- **Files:** `apps/web/views.py` (`send_auto_apply_draft`, `edit_auto_apply_draft`, `auto_apply_queue`); `templates/web/_auto_apply_queue_list.html`; `apps/auto_apply/tasks.py` (`submit_auto_apply_draft`); `apps/auto_apply/models.py` + `apps/auto_apply/migrations/0010_autoapplydraft_submitted_answers_snapshot.py`; `apps/web/tests/test_auto_apply_views.py`; `apps/auto_apply/tests/test_tasks.py` (or existing submit task tests).
- **Approach:** entries with `needs_confirmation` require an explicit per-field confirm checkbox; saving the form no longer implicitly sets `user_confirmed` for them. Send view and submit task both refuse a draft with any unconfirmed `needs_confirmation` field. On send, write `submitted_answers_snapshot` (value, source, provenance detail, tier, classifier version) in the same atomic update that sets SENDING. Bump `answers_schema_version` to 2.
- **Test scenarios:** draft with unconfirmed T0 field cannot be sent (view) and cannot be submitted (task invoked directly); explicit confirm then send succeeds; plain edit+save of such a field does not confirm it; snapshot recorded exactly once with provenance; drafts with no `needs_confirmation` entries behave exactly as today (existing send/edit tests unmodified).
- **Verification:** `SendAutoApplyDraftTests`, `EditAutoApplyDraftTests`, `DraftReviewRegressionTests` green.

## Dependency order
U1 → U2 → (U3, U4 in parallel) → U5 → U6.

## Risks
- Re-routing `resolve_field_answers` is a core-path refactor; mitigated by characterization-first and keeping the LLM/option-constraint code untouched.
- The tier regexes are safety-critical; mitigated by the labelled corpus and higher-tier-wins rule. Corpus must be seeded from real labels (only one user exists in the dev DB, so it will be small; add hand-written variants).
- `parse_resume` and `set_resume` save Profile via `post_save` → rematch; provenance work must not add saves.
- Moving `QuestionCategory` can break imports; the re-export shim and unchanged category tests guard it.

## Verification (end to end)
1. `DATABASE_URL=postgres://jobborg:jobborg@localhost:5432/jobborg DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test apps.accounts apps.auto_apply apps.web` — all green, with the pre-existing `test_answer_resolution`, `test_drafting_service`, `test_auto_apply_views` unchanged.
2. `python manage.py makemigrations --check` shows no drift; both new migrations apply on the dev DB.
3. Manual: with a seeded learned T0 AnswerBank row, generate a draft, confirm the queue shows the field flagged, Send is refused until the explicit confirm, and the snapshot is stored after send. With no seeded rows, a draft is byte-identical to before.
