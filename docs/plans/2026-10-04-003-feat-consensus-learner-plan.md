---
title: "feat: Consensus learner, Learning tab and bulk confirm (Phase 3 of Profile Overhaul, epic #117)"
type: feat
status: active
origin: docs/plans/2026-10-03-2300-consolidated-requirements.md
depth: deep
---

# Phase 3: Consensus learner (epic #117)

Work goes on branch `feat/profile-learning`, already created and stacked on `feat/profile-answers-ui` (PR #133). A tracking issue is created first.

## Context

The user's goal is: *if a question was answered before, deduce the answer instead of asking again.* Phase 2 delivered the explicit half of that goal ("remember on review") and started recording what was submitted (`AnswerObservation`). Nothing reads those observations yet. Phase 3 adds the reader, a consensus learner.

**Finding from re-reading the code that reshapes Phase 3:**
- Phase 2's remember box is **pre-ticked for T2** (factual) answers. Any T2 answer the user edits therefore becomes a user row on the first save.
- The user decided that **unticking it means "don't remember"**, and the learner respects that.
- So consensus learning rarely matters for T2. Its real value is **T0/T1**: legal and commercial answers, where "Always use this answer" is unticked by default and users retype the answer on every application.
- **EEO self-identification is the headline case**: gender, veteran status, disability and race. The LLM hard-excludes these questions (`apps/auto_apply/llm/categories.py` `HARD_EXCLUDED_CATEGORIES`), so they arrive blank on every draft. `_carry_forward_confirmed_answers` in `apps/auto_apply/services/drafting.py` only helps retries of the same job.
- Other T0/T1 cases are notice period, start date, relocation, and prior-employment questions.

**Dev DB check (read-only):**
- There are 0 observations so far, because Phase 2 is unmerged.
- Only 6 questions have the same user-confirmed answer on 2+ sent jobs.
- Historic `user_confirmed` is set by *any* review save, so it is unreliable evidence. **Phase 3 does not seed from history** and starts learning from new sends.

**Decided by the user:**
- Learn after the same answer on 2 distinct jobs.
- Every learned answer is held for confirmation in every tier, and none is auto-submitted.
- T0/T1 can be promoted to a locked answer through an explicit action.
- A learning on/off switch.
- Shadow metrics are measured only.
- An unticked remember box is excluded from learning.
- Bug #129 (the LLM gate keys on category, not tier) stays a separate PR.

## Design decisions

1. **Evidence = answers the user authored.** An observation counts only if:
   - **`user_edited`:** the user typed or changed the value. The edit view already sets `entry["user_edited"]` (`apps/web/views.py`, `edit_auto_apply_draft`), but `record_observations` does not store it.
   - **not `remember_declined`:** the user did not untick a pre-ticked T2 remember box. This flag is new and is set in the edit view when the remember UI was offered checked and the box is absent from the POST.
   - **eligible field:** the field is not textarea or file, the value is not blank, and `job_id` is not null.

   Confirming a held learned prefill, LLM guesses that were merely re-posted, and typed-fact values are all **neutral**: they are neither evidence for nor against. Bulk confirm can therefore never make learning confirm itself.
2. **Consensus rule, fail closed.**
   - Group eligible observations by `(question_key, scope)`. The scope is the observation's `job_region` when `question_semantics.is_location_sensitive(question_text)`; a sensitive question with an empty region is skipped. Otherwise the scope is `""`.
   - Take the most recent observation per distinct **employer**, falling back to `job_id` when the employer is empty, so twin postings from one company don't count twice.
   - The two most recent agreeing employers make a consensus. Values are compared normalized (casefold, collapsed whitespace, sorted lists); the stored value is the most recent submitted form.
   - Disagreement or too few employers means no write.
   - **Withdrawal:** if the newest eligible observation disagrees with an existing learned row, that row is deleted with history (`delete_answer(row, deleted_by="learner_withdraw")`). A confidently wrong prefill is worse than none.
3. **Writes go through the existing precedence.**
   - The learner calls `write_answer(..., AnswerBank.Source.LEARNED, question_key=obs_key, scope_region=..., confidence=..., source_detail={"origin": "consensus", "employers": [...], "job_ids": [...], "learner_version": LEARNER_VERSION})` in `apps/accounts/services/answer_resolver.py`.
   - `write_answer` already refuses to overwrite unexpired user or locked rows and replaces learned or imported rows with history.
   - An identical existing learned value is a no-op. Keys covered by typed facts (`typed_facts.resolve(...).covered`) and `legacy:*` keys are skipped.
4. **The resolver holds every learned row.**
   - In `resolve_answer`'s bank branch, `needs_confirmation = (tier in T0/T1 and source != USER) or source == LEARNED`.
   - When `profile.learning_enabled` is False, learned rows are skipped entirely: no prefill, rows kept. Off means "don't learn and don't use learned answers".
   - The existing send gate (`unconfirmed_fields`, views + `apps/auto_apply/tasks.py`) then blocks unconfirmed learned answers with no new gate code.
5. **Suggestions only for T0/T1.**
   - The learner also get-or-creates a `pending` `ProfileSuggestion` offering to promote the answer to a locked user answer. Accepting it calls `write_answer(source=USER, is_locked=True)`.
   - The queue's existing "Always use this answer" tick already promotes a confirmed held entry, through `remember_reviewed_answers`, `_deliberate` and `old.needs_confirmation`. The Learning tab is the second route.
   - Dismissing a suggestion, or "Forget" on a learned row, stores a `rejected` suggestion keyed by `value_fingerprint`. The learner never re-proposes that value for that key and scope.
6. **Shadow metrics are per-observation facts.**
   - `record_observations` stores `learned_value`: the value of a learned prefill, taken from the entry's `confirmed_from`/`provenance` when its origin is `answer_bank` and its source is `learned`. Otherwise it is null.
   - Bulk confirmations are marked with provenance origin `draft_review_bulk`, so a report can separate blind bulk confirms.
   - Precision per key = observations whose submitted value equals `learned_value` / observations with a `learned_value`, with a Wilson lower bound.
   - It is reported by a management command and enforces nothing.
7. **Triggers.** All three call one idempotent `learn_for_profile(profile, *, dry_run=False)`:
   - after send, via `transaction.on_commit` (observations are written inside the send view's atomic block);
   - nightly, via a Beat sweep over profiles with observations in the last 25 hours plus pending-suggestion expiry (90 days). No last-run marker is needed;
   - on demand, via an "Improve my profile" button.
8. **No rematch.**
   - The switch saves with `update_fields=["learning_enabled"]`. `learning_enabled` is not in `MATCHING_PROFILE_FIELDS` (`apps/matching/services.py`), so the signal in `apps/matching/signals.py` ignores it.
   - Learner writes touch only AnswerBank and suggestions. `write_answer` and `delete_answer` already invalidate the questions-panel cache.
9. **Where things are shown.**
   - The Learning tab (`/profile/learning/`) is the home of learned rows and suggestions.
   - The Answers page's custom-answer list (`_answer_rows` in `apps/web/views_profile.py`) excludes `source=learned` rows and shows a one-line link: "N learned answers — review on the Learning tab". This amends FR1.1 (the tab ships now) and FR1.5.
10. **Layering.** The learner lives in `apps/accounts` and imports nothing from `apps.auto_apply` or `apps.web`, matching Phase 2's leaf rules. `LEARNER_VERSION` is stored on rows and suggestions; bump it when the rules change.

## Implementation units

### U1. Schema: accounts migration 0013
- `apps/accounts/models.py`:
  - `Profile.learning_enabled` (default True).
  - `ProfileSuggestion`: profile, question_key, scope_region, question_text, value JSON, value_fingerprint, tier, evidence JSON, confidence, status `TextChoices` (pending/accepted/rejected/expired), learner_version, created_at/updated_at/resolved_at, `UniqueConstraint(profile, question_key, scope_region, value_fingerprint)` named `uniq_profilesuggestion_...`, index `(profile, status)`.
  - `AnswerObservation`: `RenameField was_edited → user_confirmed` (what it actually means), plus new fields `user_edited`, `remember_declined` and `learned_value` (JSON, null).
- `apps/accounts/admin.py`: suggestions admin, read-mostly.
- Tests in `apps/accounts/tests/test_learning_models.py`:
  - defaults;
  - fingerprint uniqueness;
  - the migration applies with existing rows.

### U2. Capture: edit view flags, observations, resolver rule
- `apps/web/views.py` `edit_auto_apply_draft`: set `entry["remember_declined"]` per design decision 1. Extract the per-entry confirm transition into `apps/auto_apply/services/confirmation.py` as `confirm_entry(entry, origin="draft_review")`, and reuse it in the edit view.
- `apps/auto_apply/services/answer_memory.py` `record_observations`: write `user_confirmed`, `user_edited`, `remember_declined` and `learned_value`.
- `apps/accounts/services/answer_resolver.py`: the learned-always-held rule, and the `learning_enabled` skip.
- Copy change in `templates/web/_auto_apply_queue_list.html`: the "Needs your confirmation" aria text currently says legal or commercial only. Make it "learned or imported".
- Execution note: write failing resolver and NFR2 tests first, because this is the safety line.
- Tests in `apps/accounts/tests/test_answer_resolver.py`, `apps/auto_apply/tests/` (observation tests) and `apps/web/tests/test_answer_memory_views.py`:
  - **Learned rows:** a learned T2 row resolves with needs_confirmation True, and learned T0/T1 too.
  - **Unchanged:** user rows of any tier, and imported T2 rows.
  - **Switch off:** with learning disabled, learned rows don't resolve and the LLM path is reached as if absent.
  - **NFR2:** a draft holding a learned T2 value can't be sent (view) or submitted (task called directly).
  - **Observation flags:** an edited field gives `user_edited`; an unticked pre-ticked T2 box gives `remember_declined`; an unticked T0 "Always use" box does *not* give `remember_declined`; a confirmed-unedited learned prefill gives `learned_value` set and `user_edited` False.
  - **Refactor:** `confirm_entry` produces identical entries to the old inline code (characterization).

### U3. Learner service
- `apps/accounts/services/learning.py` (new): `learn_for_profile(profile, dry_run=False) -> LearnReport`, which reports written, withdrawn, suggested and skipped counts with reasons.
- It does one observation query per profile, plus `load_bank_rows(profile)` and the profile's suggestions.
- It reuses `normalize_question_key` (keys are already stored), `question_semantics.is_location_sensitive`, `typed_facts.resolve` for the coverage check, `write_answer`/`delete_answer`, and `classify_tier`/`higher_tier`.
- Pending suggestions older than `LEARNING_SUGGESTION_TTL_DAYS` become expired.
- Tests in `apps/accounts/tests/test_learning.py`. Each regression shape gets a named test:
  - **Consensus counting:**
    - two employers agree → learned row with evidence;
    - one employer with two jobs → nothing;
    - disagreement → nothing;
    - A,A,B (newest differs) → nothing, plus withdrawal of an existing learned A;
    - A,B,B → B replaces A with history.
  - **Ignored evidence:**
    - null job, textarea, file and blank values;
    - `remember_declined`;
    - non-edited observations: an LLM re-post, or a confirmed learned prefill (no self-reinforcement);
    - typed-fact-covered and legacy keys.
  - **Precedence:** an existing user row, locked or not → no write and no suggestion.
  - **Suggestions:** T0/T1 → learned row + exactly one pending suggestion, and a rerun is idempotent; T2 → no suggestion; a rejected fingerprint blocks re-proposal, and a new consensus value is allowed.
  - **Location-sensitive scoping:** per-region rows; empty region skipped.
  - **Modes:** `learning_enabled=False` → no writes; dry_run writes nothing but reports.
  - **Expiry:** an expired suggestion is marked.
  - **Layering:** an import-boundary test (no auto_apply/web imports).

### U4. Tasks, triggers, settings, report
- `apps/accounts/tasks.py`: `apps.accounts.learn_for_profile` and `apps.accounts.sweep_learning`. Follow the `parse_resume` pattern: lazy import, log `DoesNotExist` and return. The sweep handles each profile in try/except and logs; it is capped by `LEARNING_SWEEP_BATCH_SIZE`.
- `config/settings/base.py`: add `env.int` `LEARNING_MIN_DISTINCT_EMPLOYERS=2`, `LEARNING_SWEEP_BATCH_SIZE`, `LEARNING_SUGGESTION_TTL_DAYS=90`, and the Beat entry `learning-sweep-nightly` (crontab 03:30).
- `apps/web/views.py` `send_auto_apply_draft`: inside the atomic block, add `transaction.on_commit(lambda: learn_for_profile.delay(profile_id))` after `record_observations`.
- `apps/accounts/services/shadow_metrics.py` + `apps/accounts/management/commands/learning_report.py`. Options: `--user`, `--dry-run-learn` (runs the learner in dry-run and prints what it would write).
- Tests in `apps/accounts/tests/test_learning_tasks.py` and `test_shadow_metrics.py`:
  - the task is scheduled after commit only, and not on a lost send race;
  - a missing profile is handled;
  - the sweep picks recent profiles only, survives one failure, and respects the batch size;
  - the Beat entry exists;
  - shadow precision: 3 matches + 1 mismatch = 0.75, keys without `learned_value` excluded, bulk vs per-field split;
  - **end-to-end** (eager Celery): send edited drafts for two employers → learned row exists → the next draft's entry is prefilled and held.

### U5. Confirm all learned answers
- `apps/web/views.py`: new `confirm_learned_answers(request, pk)`, a POST scoped to `user=request.user, status=DRAFTED` (404 otherwise).
- It applies `confirm_entry(origin="draft_review_bulk")` to entries with `needs_confirmation`, a non-blank value and provenance origin `answer_bank`. LLM, blank and typed entries are untouched. It saves `update_fields=["answers", "updated_at"]` and does not promote anything.
- `apps/web/urls.py`: `auto-apply/drafts/<int:pk>/confirm-learned/`.
- Template: a button showing the count ("Confirm 3 learned answers"), computed in `auto_apply_queue`; hidden when the count is 0.
- Tests in `apps/web/tests/test_auto_apply_views.py`:
  - learned entries confirmed, LLM and blank entries untouched;
  - entries identical to per-field confirmation except the origin;
  - another user's draft → 404, and a non-drafted draft → 404;
  - send is unblocked only when nothing else is held;
  - button hidden at zero.

### U6. Learning tab
- `apps/web/views_profile.py`, `apps/web/forms_profile.py` (`LearningSettingsForm`), `apps/web/urls.py`.
- New template `templates/web/profile_learning.html`. Also update `templates/web/_profile_tabs.html` (third tab) and `templates/web/profile_answers.html` (learned rows excluded, plus the count link).
- **Page sections:**
  - **Switch:** the on/off form.
  - **Pending suggestions:** "Use this answer from now on" (locked user row; suggestion accepted) and "Dismiss" (rejected).
  - **Learned answers:** shows evidence (employers, count), with "Use from now on" (unlocked user row for T2, locked for T0/T1) and "Forget" (`delete_answer` + rejected suggestion).
  - **Bulk actions:** "Forget all learned answers", and "Improve my profile" (synchronous `learn_for_profile`, result via messages; disabled when off).
- **Ownership and empty state:** every lookup is scoped via `get_object_or_404(..., profile=request.user.profile)`, mirroring `_own_row`. The empty state is one sentence on how learning works.
- Tests in `apps/web/tests/test_learning_views.py`:
  - lists only the user's own rows;
  - each action's DB effect;
  - dismiss/forget stops re-proposal;
  - the toggle schedules no rematch (patch `schedule_rematch`);
  - run-now creates a row from consensus data;
  - foreign ids → 404, anonymous → login redirect;
  - the Answers page excludes learned rows and shows the link.

### U7. Docs
- Amend `docs/plans/2026-10-03-2300-consolidated-requirements.md`:
  - FR8.2: 2 distinct employers, authored evidence, no history seed;
  - FR8.4: learned rows held in every tier; T0/T1 promotion explicit;
  - FR8.5: no embedding context yet;
  - FR8.6: measured only;
  - FR8.8: off also stops using learned rows;
  - FR1.1/FR1.5: the Learning tab ships now; learned rows live there.
- Then the tracking issue, the PR stacked on #133, and the epic progress note. The PR notes that `worker` and `beat` must be restarted after deploy.

## Order

U1 → U2 → U3 → U4 → U6. U5 needs only U2. U7 is last.

## Deferred (not in this phase)

Not in this phase:
- the #129 LLM tier gate (separate PR, per the user);
- embeddings and reworded questions (#130, Phase 4b);
- import (Phase 4);
- T2 auto-apply behind the precision gate (Phase 5);
- seeding from historic drafts;
- expiry of learned T0/T1 rows (always held anyway);
- a metrics dashboard UI.

## Verification

1. Run `DATABASE_URL=postgres://jobborg:jobborg@localhost:5432/jobborg DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test --noinput`. It must be fully green, and existing Phase 2 tests may change only where `was_edited` was renamed or learned-T2 confirmation is now intended.
2. `python manage.py makemigrations --check` is clean, and 0013 applies to the dev DB.
3. Manual, in a rolled-back shell transaction or with a test user (never a real account):
   - send drafts for two employers with the same edited EEO answer;
   - `learning_report --dry-run-learn` shows it, and the learner writes a learned row and a pending suggestion;
   - the next draft shows it prefilled with "Needs your confirmation"; Send is refused until it is confirmed; "Confirm learned answers" clears it;
   - on the Learning tab, "Use from now on" locks it, so the next draft fills it without a hold;
   - turning learning off stops the prefill.
