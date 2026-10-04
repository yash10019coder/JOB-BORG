---
title: "feat: Profile answers UI, typed-fact resolver and answer memory (Phase 2 of Profile Overhaul, epic #117)"
type: feat
status: active
origin: docs/plans/2026-10-03-2300-consolidated-requirements.md
depth: deep
---

# feat: Profile answers UI, typed facts and answer memory (Phase 2, epic #117)

Branch: `feat/profile-answers-ui` (built on Phase 1, PR #125). Tracking issue: #128 (epic #117).

## Context

Phase 1 built AnswerBank, the tier classifier, `resolve_answer()` and the confirmation gate. Phase 2's exit criterion is "users can maintain every auto-apply answer from one page", and the user has now sharpened the goal: **if a question has been answered before, the system should deduce the answer from earlier applications instead of the user typing it again.**

Evidence from the dev DB (1 dominant user, 258 drafts; repeated attempts at the same form inflate counts): of 1,897 non-standard answer entries, 701 distinct normalized questions; 211 recur in 2+ drafts and account for 1,407 entries (74%); about half of those (707) ended blank or needs-review. Only 91 entries were ever user-confirmed, and nothing writes review edits back as reusable answers today. That recurring-and-blank pool is the target.

Defects found in research that Phase 2 must absorb:
- **Drafting crashes** for any user with a salary band: the override reads `job.target_locations_normalized`, which `Job` lacks (tests fake it). Fixed here by folding salary into the resolver.
- **Typed facts never consulted.** `Profile.authorization_for_country` has no callers; nothing maps stored option keys (`yes_authorized`) to live labels.
- **"Country" is the phone dial-code picker** (244 options like `"United States +1"`), the most common non-standard question; the LLM mostly fails it.
- **Common words resolve to countries** (`alpha3_for_country("us"/"no"/"it"/"me"/"be"/"is")` → USA/NOR/ITA/MNE/BEL/ISL, verified): country detection must be anchored and case-sensitive.
- **The key is brittle:** normalized text + options hash splits rewordings, employer-name variants and decorations, and the options hash splits identical facts across option phrasings.
- Matching never reads visa/citizenship/salary-by-region/contact fields (verified), so FR1.6's rematch rule is wrong; Answers saves never rematch.
- `ExplicitAnswersForm` bugs #126/#127 disappear when it is replaced.

## User decisions (final)

1. Two server-rendered pages: `/profile/` (search criteria + resume) and `/profile/answers/` (contact & location, work auth, citizenship, salary, custom answers, questions panel). No Learning tab yet.
2. Contact/link fields plus new city/country/address/timezone live on the **Answers** tab. Answers saves never rematch.
3. Legacy `ExplicitAnswer`: one-time backfill into AnswerBank (`legacy:*` rows), then read-only until Phase 5. No literal dual write.
4. Quick-fill for work-auth/citizenship/salary rows **links to the setting**; quick-fill only writes custom answers.
5. Salary crash fixed inside Phase 2 (no hotfix on #125).
6. **Answer memory is the priority:** remember-on-review and observation capture ship in Phase 2; the consensus learner is the next phase, ahead of import (reverses the earlier "learning is P1 after Phase 2" ordering only in sequence, not in safety rules).
7. NFR2 stays absolute: learned/imported T0/T1 answers are prefilled but never sent without explicit confirmation; T2 learned answers do not send silently until the shadow gate passes.

## Defaults I chose (flag on review)
- **Location handling:** location-sensitive custom answers (wording about relocation, on-site/hybrid/in-office, commuting, or a named country) are stored **per region** (`scope_region`, blank = everywhere) and only used for jobs in that region or when the user ticks "applies everywhere". Everything else is global.
- **Remember defaults:** T2 answers are remembered by default (checkbox pre-ticked); T0/T1 answers are remembered only if the user ticks "Always use this answer", which stores a locked user row.
- **Question key:** normalized text only (options no longer part of the key); the stored value is mapped onto the live options at resolve time.

## Scope boundaries

In scope: rematch guard, regions/salary_bands move, contact columns, `question_semantics` + `typed_facts`, key redesign + region scope, resolver rewrite, drafting rewire, backfill, split forms/views/templates, custom-answer CRUD, remember-on-review, observation capture, questions panel + quick-fill + cache.

### Deferred to follow-up work
- **Next phase (Learning, before Import):** consensus learner over observations (same answer across 2+ distinct jobs → learned row; T0/T1 confirm-each-time, T2 prefilled-and-reviewed until the gate), suggestions/Learning tab, per-draft "confirm all remembered answers" button, shadow-mode metrics (the observation table already has the (user, job, question) unit), then intent rules and embeddings (propose only).
- General NFR2 gap in the LLM path (category-gated, not tier-gated: "Are you a US citizen?" can be LLM-answered with `needs_review=False`; gating all T0-generic would also flag School/Discipline). Phase 2 only closes it for citizenship/location-membership questions typed facts recognise. **File as an issue.**
- Structured address columns; "City and State"/"Postal code" stay `None`.
- Import (resume/LinkedIn/GitHub), T2 auto-apply gate, dropping `ExplicitAnswer` (later phases). GDPR deferred (NFR8).
- Requirements-doc updates (applied in U13): FR1.6 rematch rule, panel module location, phase order, `scope_region`, `AnswerObservation`, key redesign, consensus threshold 2.

## Key technical decisions

- **Leaf layering.** Pure `apps/accounts/question_semantics.py` (regex + `apps.locations.engine`): split/negation rules, country-mention extraction, contact/citizenship/salary/location-sensitivity detectors, option pickers. `apps/accounts/services/typed_facts.py` reads the Profile and returns `TypedFact(kind, covered, value, provenance_key, detail, reason)`. `apps.accounts` never imports `apps.auto_apply` or `apps.web`; the import-boundary test covers both new modules.
- **Move `regions.py`/`salary_bands.py` to `apps/accounts/`** with re-export shims at the old `apps/web` paths (like `tiering` → `llm/categories.py`); delete dead `_map_country_to_region` and the duplicate band-label helper.
- **Key redesign.** `normalize_question_key(text, employer=None)`: lowercase, punctuation collapsed, plus conservative decoration stripping only (`*`, "(optional)"/"(required)", leading "please", trailing colon) and the employer's name removed (known from the job). Verbs ("do you have", "are you") are **not** stripped: a false merge is worse than a miss. Options leave the key; `source_detail.options_hash` records them for audit. The stored value is mapped onto the live options at resolve time (exact casefold, or yes/no polarity); no match → blank + `needs_review`. Dev DB has 0 AnswerBank rows; a data migration still rewrites any existing keys (strip `#hash`, dedupe, keep highest precedence) for safety.
- **Narrative (open-ended) questions.** Detected by field type (`textarea` in the form schema) when no typed fact/regex rule matched, not by regex. They are never remembered by default (remember checkbox unticked) and never carried across employers; same-employer reruns may prefill, always `needs_review`. Employer-name stripping in the key applies only to short factual fields (non-`textarea`), and a stored answer that contains the employer's name is never reused for a different employer. Unmatched short-text/select questions keep the T0 default; the remember control then offers a clear one-click "Always use this answer" instead of silently doing nothing. (Found by walking "What's the most interesting thing you have encountered?" and "Why do you want to work at Canonical?" through the classifier: both are `generic`/T0.)
- **Region scope.** `AnswerBank.scope_region` (CharField, blank = everywhere); unique `(profile, question_key, scope_region)` replaces `(profile, question_key)`. Resolver prefers the row matching the job's region, then the blank-scope row. For location-sensitive questions a blank-scope row counts only if the user chose "applies everywhere" (stored in `source_detail.applies_everywhere`); an unknown job region means no resolution. Location-sensitive detection reuses the anchored country extractor plus `relocat|on-?site|in[- ]office|hybrid|commut`.
- **Resolver order:** typed facts → AnswerBank (region-scoped row, then global) → legacy `legacy:*` bank rows (only when no typed fact `covered`) → LLM → blank. `legacy_lookup` injection removed; `answer_resolver.legacy_fallback_hit` stays. Optional `bank_rows` preload for the panel.
- **Work-auth semantics.** `_AUTHORIZATION_BY_VISA_STATUS` → `(authorized, needs_sponsorship)`, `None` = unknown: citizen/PR/work_permit (True, False); requires_sponsorship (None, True); not_authorized (False, None); h1b/opt/o1/tn/e3 (True, None) but None when the question has a restriction qualifier ("without restriction", "any employer"); other (None, None). Citizenship overrides. Fixes the old `not_authorized → no sponsorship needed` mapping. Country = one named in the question, else the job's (`job.location_country`); two distinct → `None`; named-but-unresolvable → `None`.
- **Country extraction:** anchored after `in|within|of|from`, case-sensitive, allowlist for 2–3 letter tokens (`US, U.S., USA, UK, UAE`), right-to-left prefix trimming only.
- **Option mapping (none exists today):** `pick_yes_no` (exactly one option of the polarity, else `None`), `pick_country_option` (strip trailing `+NN`/parentheticals, compare alpha-3 per option; if `phone` starts with `+` the dial code must agree), `pick_exact` (casefold). Always returns the verbatim option so `_enforce_option_constraint` passes.
- **Salary:** expectation wording only (excludes current/previous/last salary and yes/no phrasings); job country → region → band; `""`/`other` = not covered; **`_profile_derived_answer` (min_salary fallback) removed** (FR4.3/4.4: a raw number in `preferred_currency` is an FX guess; dev user has 2,000,000 INR).
- **Contact columns:** `location_city`, `location_country` (alpha-3), `mailing_address` (500 chars), `timezone` (IANA select; add `tzdata`). Added to `profile_fields.SIMPLE_FIELDS`. User-typed values are `source=user`, so `needs_confirmation` is False regardless of tier. Tiering tweaks, `TIERING_VERSION="v2"`: `petition`/`employment-based status` → WORK_AUTHORIZATION (the BambooHR petition question is currently `GENERIC`/T2 and LLM-answerable), T2 patterns for "what/which country … located/live/work", corpus rows.
- **Remember-on-review.** `edit_auto_apply_draft` gets a `remember__{i}` checkbox per edited/confirmed non-file, non-standard field. T2: pre-ticked, writes `write_answer(source="user", is_locked=False)`. T0/T1: unticked by default; ticking ("Always use this answer") writes a locked user row. Location-sensitive questions store `scope_region` = the job's region unless the user ticks "applies everywhere". Value comes from the validated cleaned value, never from posted provenance. `write_answer` already handles precedence/history.
- **Observation capture.** New `AnswerObservation` (profile FK CASCADE, job FK SET_NULL, employer id, `question_key`, `question_text`, `value` JSON, `tier`, `provenance_source`, `provenance_origin`, `was_edited`, `created_at`; index `(profile, question_key)`). Written in the same transaction as the send, from the exact values in `submitted_answers_snapshot`, skipping FILE/standard fields. This is the ground truth for the next phase's consensus learner and shadow metrics ((user, job, question) unit, FR8.6).
- **Legacy rows** live in AnswerBank under `legacy:work_authorization`/`legacy:sponsorship` (T0), `legacy:salary_expectation` (T1), routed by the same split as Phase 1; stored `source_detail.answer_bool` is mapped to options via `pick_yes_no`. Legacy salary resolves only when its recorded region equals the job's. Conflicts with typed facts are **non-blocking** (log `answer_resolver.legacy_conflict`, add `provenance["conflict"]`, banner on Answers tab) because legacy rows are country-agnostic. Legacy rows are read-only with Delete.
- **Rematch guard.** `MATCHING_PROFILE_FIELDS` in `apps/matching/services.py`; signal returns early when `update_fields` is given and disjoint from it; Answers form saves with explicit `update_fields`; drift test vs `profile_snapshot()`.
- **Panel module** lives in `apps/auto_apply/services/questions_panel.py`; only the cache key + `invalidate_questions_panel` live in accounts (`panel_cache.py`). Key `profile_questions_panel:v1:{user_id}`, 1 h TTL, `transaction.on_commit` delete; invalidated from `_persist_draft`, `write_answer`, custom delete, quick-fill, Answers save, remember-on-review.
- **Visa repeater no-JS data loss:** rows are JS-seeded today so a no-JS POST wipes `visa_status_by_country`; the new template renders saved rows server-side.
- **Backfill migration** lives in `apps/auto_apply/migrations/0011_backfill_explicit_answers.py` (depends on auto_apply 0010 and the new accounts migrations), frozen translation tables, noop reverse, idempotent.
- Natural landing cut: U1–U8 (resolver + data) can ship as one PR, U9–U13 (UI, memory, panel) as a second.

## Implementation units

Order: U0 → U1 → U2 → U3 → U4 → U5 → U6 → U7 → U8 → U9 → U10 → U11 → U12 → U13. U1–U3 parallelizable after U0; U5 needs U0; U6 needs U4/U5; U8 needs U6; U9 needs U3/U6/U8; U11 needs U5/U9; U12 needs U6/U7/U9.

### U0. Phase 1 leftovers (decided: fold into this phase)
- **Goal:** clear the known minor findings from PR #125 review before the resolver is rewritten on top of them.
- **Files:** `apps/accounts/services/answer_resolver.py`, `apps/accounts/services/profile_fields.py`, `apps/accounts/admin.py`, `apps/auto_apply/services/answer_resolution.py`, `apps/web/views.py`, `apps/accounts/tests/test_answer_resolver.py`.
- **Approach:**
  - `write_answer`: catch `IntegrityError` on the concurrent first create and retry as an update; an expired lower-source row must not block a lower-precedence writer (treat expired as absent for precedence); reset `category` correctly on update.
  - One shared `SOURCE_RANK` and one `is_blank` helper (currently duplicated between `answer_resolver.py` and `profile_fields.py`); put them where both can import without a cycle.
  - Replace the per-question `AnswerBank` query inside `resolve_field_answers` with one preload per draft (`bank_rows`), shared with the panel work in U12.
  - `UNCONFIRMED_ANSWERS` message wording; `Profile.field_provenance` and AnswerBank admin read-only.
  - Housekeeping (no code): confirm CI is green on the Phase 1 head, decide merge order of #125 vs #123, delete the `manualcheck` test user/job from the dev DB (user-approved cleanup only; never touch a real account), update epic #117 with Phase 1 status.
- **Execution note:** characterization-first; all existing Phase 1 tests pass unmodified before and after.
- **Tests:** concurrent first create resolves to one row (simulate with a patched `create` raising `IntegrityError`); expired learned row does not block an imported write that would otherwise be refused/allowed per precedence rules; category reset on update; downgrade case (lower source after expiry) that CodeRabbit flagged as missing; `resolve_field_answers` issues one AnswerBank query per draft regardless of question count (`assertNumQueries`).
- **Verification:** `manage.py test apps.accounts apps.auto_apply apps.web` green with no existing test edited.

### U1. Field-aware rematch guard
- **Files:** `apps/matching/services.py`, `apps/matching/signals.py`, `apps/matching/tests/test_signals.py` (new), `apps/accounts/tests/test_field_provenance.py`.
- **Tests:** full save rematches; `update_fields=["phone","updated_at"]` does not; `["target_tags","updated_at"]` does; profile creation rematches; `apply_profile_field("phone")` → 0, `("target_tags")` → 1; snapshot-subset drift test.

### U2. Move regions/salary bands; tiering tweaks
- **Files:** `apps/accounts/regions.py`, `apps/accounts/salary_bands.py` (new); shims in `apps/web/regions.py`, `apps/web/salary_bands.py`; repoint imports (`apps/web/forms.py`, `apps/web/views.py`, `drafting.py`); `apps/accounts/tiering.py`; `apps/accounts/tests/fixtures/tier_corpus.json`.
- **Tests:** region tests pass via shim; BambooHR petition → WORK_AUTHORIZATION/T0; "What country are you located in?" → T2; "Are you located in Argentina/…?" stays T0; existing category tests untouched.

### U3. Contact columns, provenance, authorization table
- **Files:** `apps/accounts/models.py` (4 columns; rewritten `_AUTHORIZATION_BY_VISA_STATUS`/`authorization_for_country`; fix stale `target_locations_normalized` docstrings), `apps/accounts/migrations/0011_profile_location_contact_fields.py`, `apps/accounts/services/profile_fields.py`, `apps/accounts/admin.py`, `requirements/base.txt`.
- **Tests:** `authorization_for_country` for all 11 statuses, citizenship override, unknown country → None, alpha-2-collision codes → None; `record_user_edits` stamps new fields; `apply_profile_field("location_country")` respects user/locked.

### U4. `question_semantics` leaf
- **Files:** `apps/accounts/question_semantics.py`, `apps/accounts/tests/test_question_semantics.py`.
- **Tests:** ambiguous phrasings from `WorkAuthorizationSponsorshipSplitTests` plus negation ("work without sponsorship"); country extraction ("in the United States for our Company" → USA; "work for us" → none; "in IT" → none; "in New Jersey" → unresolvable; "in the US and Canada" → ambiguous; slash lists); `pick_yes_no` incl. two "No…" options → None; `pick_country_option("India +91")` and "British Indian Ocean Territory +246" never IND; contact-kind table incl. exclusions (`relocat|willing|commute|applying for|office|prefer`, "state/line/zip/postal/email"); salary detector excludes "current salary"; location-sensitivity detector ("Are you willing to relocate?", "Do you work on-site in Austin?", "years of Python" → not sensitive).

### U5. Answer key redesign + region scope + observations (schema)
- **Files:** `apps/accounts/models.py` (`AnswerBank.scope_region`, constraint `uniq_answerbank_profile_question_key_scope`; new `AnswerObservation`), `apps/accounts/migrations/0012_answerbank_scope_observation.py` (schema + RunPython rewriting existing keys: strip `#hash`, dedupe by precedence), `apps/accounts/services/answer_resolver.py` (`normalize_question_key(text, employer=None)`, `write_answer(..., scope_region="", min_tier=None, applies_everywhere=False)`), `apps/accounts/admin.py` (AnswerObservation read-only; `field_provenance`/AnswerBank admin read-only per CodeRabbit follow-up), `apps/accounts/tests/test_answerbank_models.py`, `apps/accounts/tests/test_answer_resolver.py`.
- **Tests:** keys equal across `*`, "(optional)", trailing colon, leading "please", employer name present/absent; keys differ for different verbs ("Do you have X" vs "Are you X"); same text different options → same key; same key + different scope_region coexist, duplicate (profile, key, scope) rejected; `min_tier` can only raise; migration rewrites a `#hash` key and merges duplicates keeping the higher-precedence row; observation row deleted with profile; history rows still append-only.

### U6. `typed_facts` + resolver rewrite
- **Files:** `apps/accounts/services/typed_facts.py`, `apps/accounts/services/answer_resolver.py`, `apps/accounts/tests/test_typed_facts.py`, `apps/accounts/tests/test_answer_resolver.py`.
- **Tests:** authorization (citizen+India job → "Yes", origin `profile.citizenship_countries`; h1b "without restrictions" → None; h1b sponsorship → None and covered so legacy not consulted; requires_sponsorship; not_authorized; no entry → falls to bank; named country beats job country; unresolvable → None); citizenship (empty list → None; `["IND"]` → No for US, Yes for India; nationality select single/dual); salary (US band; region w/o band; blank job country; band "other"; option mismatch; "current salary" unclaimed; Japan job → None; **never an FX/min_salary value**); contact ("Country" with 244 dial options → "India +91"; `+1` phone vs IND → None; city; location composite; address; timezone; membership Yes/No, city-level → None); provenance (user field → no confirmation; imported T0-phrased country → `needs_confirmation=True`; T2 "Country" → False); order (typed beats user bank row; covered-None lets user bank row through but not legacy); **bank value→option mapping** (stored "Yes" fills "Yes"/"No" and "Yes, I do"/"No, I don't" option sets; stored text with no matching option → blank); **region scope** (US-scoped relocation answer used for a US job, not for an India job; unknown job region → not used; applies-everywhere row used anywhere; non-sensitive global row used everywhere); legacy (routing, `answer_bool` mapping, code `other` → None, free-text like Phase 1, salary region match, conflict logged without forcing review). Import-boundary test extended.

### U7. Wire into answer resolution + drafting (crash fix)
- **Files:** `apps/auto_apply/services/answer_resolution.py` (`job` param, drop ExplicitAnswer query/`legacy_lookup`/`_profile_derived_answer`/category map, `PROFILE_FACT_REASON`, unanswered citizenship/membership → blank and never to the LLM), `apps/auto_apply/services/drafting.py` (pass `job` and employer, delete salary override and web imports, invalidate panel from `_persist_draft`), tests in `apps/auto_apply/tests/test_answer_resolution.py`, `test_drafting_service.py`.
- **Execution note:** characterization-first; run both suites before changing and keep every test that doesn't touch removed paths unchanged.
- **Tests:** real `Job(location_country="US")` + US band → label, reason `profile_fact`, origin `profile.salary_by_region.US` (replaces the faked attribute); empty `location_country` → no crash, blank + review; typed beats a learned salary row with `needs_confirmation=False`; draft creation invalidates panel cache (`captureOnCommitCallbacks`); "Country" never calls the LLM; learned T0 bank row still held until confirmed (NFR2).

### U8. ExplicitAnswer backfill + read-only legacy
- **Files:** `apps/auto_apply/migrations/0011_backfill_explicit_answers.py`, `apps/auto_apply/admin.py`, `apps/auto_apply/tests/test_explicit_answer_backfill.py`.
- **Mapping:** work_authorization `yes_*`→True, `no_need_sponsorship`→False, `no_sponsorship_needed`→False, `other`→None; sponsorship `no`→False, `yes_*`→True, `other`→None; salary band key in exactly one region → that region's label + `region`, else verbatim; OTHER → `legacy:other` (display only); unknown text → verbatim, `answer_bool=None`. Every row `source=user`, `is_locked=True`, correct tier, `source_detail` with origin/legacy_category/legacy_code/answer_bool/region/explicit_answer_id; idempotent, never overwrites.
- **Tests:** dev-shaped trio → three rows (`100-125k` → `$100,000 – $125,000`, region US); idempotent; existing row not overwritten; per-user isolation; admin read-only; end-to-end backfilled `no_need_sponsorship` answers "legally authorized to work in the United States?" `["Yes","No"]` → "No" for a profile with no USA entry.

### U9. Split forms, views, URLs
- **Files:** `apps/web/forms_profile.py` (`ProfileSearchForm`, `AnswersSettingsForm`, `CustomAnswerForm`, `QuickFillForm`), `apps/web/views_profile.py`, `apps/web/urls.py`, `apps/web/forms.py` (remove `ExplicitAnswersForm`, `WORK_AUTH_CHOICES`, `SPONSORSHIP_CHOICES`, old `ProfileForm` or re-export), `apps/web/views.py` (drop `explicit_answers`, `_profile_form_context`).
- **Routes:** `profile/` (search; save → recommendations + rematch); `profile/answers/` (save → same page + message, explicit `update_fields`, no rematch; invalid → 200 re-render keeping input); `profile/answers/custom/` create, `…/<pk>/` update, `…/<pk>/delete/` (all `AnswerBank.objects.get(pk, profile=request.user.profile)` → 404; tier override may only go higher; delete appends history `superseded_by_source="user_delete"`; legacy keys delete-only; form offers region scope for location-sensitive wording); `profile/explicit-answers/` → `RedirectView` to `profile_answers`, keeping name `explicit_answers`.
- **Tests:** search POST without phone/visa leaves them untouched; Answers POST without target fields leaves matching fields and rematch count 0; duplicate country / unknown status errors; no-JS POST never wipes visa entries; citizenship + salary round-trips; invalid POST keeps input (#127); new user sees no sponsorship prefill (#126); cross-user pk → 404; CSRF enforced; lock toggle; per-field provenance stamped; Answers save invalidates cache; old URL redirects.

### U10. Templates
- **Files:** `templates/web/_profile_tabs.html`, `templates/web/profile_form.html` (search only + messages block), `templates/web/profile_answers.html` (sections `#contact`, `#work-auth` with server-rendered rows + one blank row, `#citizenship` labelled "Countries whose passports you hold", `#salary`, custom answers in `<details>` per category with "Auto-apply will use: value — source, confidence" + lock toggle + region scope, legacy box + conflict banner, panel), `templates/web/_auto_apply_queue_list.html` ("From your profile (…)" hint, conflict note), `templates/base.html` (rename dead `.salary-region-tabs` CSS to `.profile-tabs`).
- **Tests:** `assertContains` for each anchor, server-rendered visa rows, messages; no `|safe` on user data.

### U11. Remember-on-review + observation capture
- **Files:** `apps/web/views.py` (`edit_auto_apply_draft`: parse `remember__{i}`/`everywhere__{i}`; `send_auto_apply_draft`: write observations inside the same atomic block after the conditional update succeeds), `templates/web/_auto_apply_queue_list.html` (checkbox per editable non-file, non-standard field; label differs by tier), `apps/auto_apply/services/confirmation.py` (observation builder from the snapshot), `apps/web/tests/test_auto_apply_views.py`, `apps/auto_apply/tests/test_confirmation.py`.
- **Approach:** remember writes use the validated cleaned value, the job's employer for key normalization, `scope_region` from the job's region for location-sensitive questions unless `everywhere`, `source_detail={"origin":"review","draft_id":…}`. A failed `write_answer` (e.g. locked higher row) never blocks the edit save; surface a message.
- **Narrative tests:** a `textarea` question with no rule match is not remembered by default; ticking remember on it still only prefills for the same employer and always as `needs_review`; "Why do you want to work at Canonical?" answer (contains "Canonical") is never prefilled for DoiT; employer-stripped keys apply to a short-text question ("Are you willing to relocate to work at Acme?" equals the same question for Beta) but not to a `textarea`.
- **Tests:** T2 edit with box ticked writes a user row and the next draft for a different job with the same question prefills it; unticked writes nothing; T0/T1 unticked writes nothing, ticked writes a locked user row; location-sensitive remember is region-scoped and does not fill a draft in another region; everywhere flag fills both; FILE/standard fields never remembered; observation rows written exactly once per send with the snapshot values, tier and provenance; a send that loses the conditional update writes no observations; observations skip blank values; cross-user isolation; **learned T0 row still held for confirmation (NFR2 unchanged)**.

### U12. Questions panel + quick-fill
- **Files:** `apps/accounts/services/panel_cache.py`, `apps/auto_apply/services/questions_panel.py`, quick-fill views in `apps/web/views_profile.py` (`profile/answers/quick-fill/` GET plain page, POST), `templates/web/_questions_panel.html`, `templates/web/profile_quick_fill.html`, `apps/auto_apply/tests/test_questions_panel.py`, `apps/web/tests/test_quick_fill_views.py`.
- **Panel:** last 200 drafts with a snapshot (`select_related("job")`), skip FILE fields, aggregate by `normalize_question_key(label, employer)` (count, last_seen, latest draft/index/entry), grouped by category; work-auth/citizenship/salary/contact rows show "answered by your settings" or "not covered (reason)" with an anchor link (no quick-fill); other rows resolve via `resolve_answer(job=latest.job, bank_rows=preloaded)` → ✓ or unanswered + quick-fill.
- **Quick-fill:** server reloads the draft by `(pk, user)` → 404; label/options/prefill derived server-side; `prefill_non_user` when the entry lacks `user_confirmed` and `provenance.source != "user"`; T0/T1 + non-user prefill requires a separate `confirm_value` checkbox, otherwise re-render with error and write nothing; enforced options require an exact option; an existing/locked target shows a conflict notice and requires `overwrite`; write via `write_answer(source="user", is_locked=True, source_detail={"origin":"quick_fill","draft_id":…,"prefill_provenance":…})`; invalidate cache; JS opens the same partial in a `<dialog>` via `modal.js`, no-JS uses the plain page.
- **Tests:** ≤200 drafts; counts/last_seen/grouping; work-auth row "answered by your settings" once a USA row exists, "not covered" before; second call issues no draft queries (`assertNumQueries`); invalidation on new draft/`write_answer`/custom delete/Answers save/remember; user value writes a locked row with `draft_id`; **T0 LLM-prefilled value without `confirm_value` writes nothing, with it writes** (NFR2/FR6.5); T2 needs no confirmation; other user's draft → 404; non-option value → error; locked target without `overwrite` unchanged; question then ✓; cold build on 200 drafts <1 s with preloaded rows.

### U13. Docs and tracking
- **Files:** `docs/plans/2026-10-03-2300-consolidated-requirements.md` (FR1.6 rematch rule; panel module location; phase order with Learning ahead of Import; consensus threshold 2; `scope_region`; `AnswerObservation`; key redesign; Open Question 10 now answered: history kept), epic #117 body status, new issue for the LLM-path T0-generic gap.
- **Tests:** none (docs/tracking only).

## Differences from the consolidated requirements (needs your sign-off)

| Doc says | This plan | Why |
|---|---|---|
| FR5.4(1)/Data Model: key = normalized text **plus hashed option set** | Key = normalized text only; value mapped to live options at resolve time | Hash splits identical facts across option phrasings; needed for cross-application reuse |
| FR1.2: contact/link fields on Tab 1 | They move to Tab 2 (Answers) with the new city/country/address/timezone | Your decision; keeps Answers saves rematch-free |
| FR1.6: Tab 2 rematches if FR2/FR3 changed | Answers saves never rematch | Verified: matching reads none of those fields |
| FR6.3/FR6.4: quick-fill maps into salary/work-auth/citizenship targets | Those rows link to the setting; quick-fill writes custom answers only | Your decision; keeps T0 values out of the panel path |
| Decision 9 / Data Model: dual write ExplicitAnswer ↔ AnswerBank | One-time backfill, then ExplicitAnswer read-only | Your decision this session; nothing writes the legacy table any more |
| FR7.4/FR7.5: legacy `ExplicitAnswer` consulted last | Resolver reads backfilled `legacy:*` AnswerBank rows instead; the table itself is no longer read | Follows from the backfill |
| FR2.5: draft mapping stays in `_AUTHORIZATION_BY_VISA_STATUS` | Same name, value shape becomes `(authorized, needs_sponsorship)`; fixes `not_authorized → no sponsorship needed` | Old values were form keys, unusable against live forms |
| FR1.7: contact facts T2 | Same, plus tiering v2 pattern tweaks | Close the BambooHR-petition and "what country are you located in" holes |
| Phase order: 3 Import, 4 Learning | Learning (consensus learner) before Import; consensus threshold 2 not 3 | Your goal: stop re-entering answers |
| FR8.1 signals / FR8.6 observations start in Phase 4 | Remember-on-review and `AnswerObservation` capture start in Phase 2 | Data accrues from day one; shadow metrics get history |
| Data Model: `AnswerBank` unique `(profile, question_key)` | Adds `scope_region`; unique `(profile, question_key, scope_region)` | Location-sensitive custom answers |
| Endpoints: `/profile/questions-panel/` GET | Panel is part of `/profile/answers/` (no separate endpoint) | Server-rendered tab; cache still per user |
| Open Question 10: keep `AnswerBankHistory`? | Treated as keep (my assumption) | Remember-on-review and deletes write history |

Not changed: tier model, NFR2, FR2–FR4 vocabularies/storage, FR5.1–5.3/5.5/5.6, FR6.1/6.2/6.5/6.6, FR7.1–7.3/7.6–7.10, Phase 5 contents.

Unaddressed doc items: FR5.4(2) embedding candidates (Phase 4+, consistent with Open Question 6); the FR7.10 "grep/guard test for direct writes to covered fields" (planned for Phase 3 when importer/learner writers exist); per-user rate limits (belong to the later import/learn endpoints).

## Phase 1 leftovers

Already absorbed by this plan: typed visa/salary facts consulted by the resolver (U6); salary-region override folded in and its crash fixed (U7); `min_salary` fallback removed (U6/U7); AnswerBank/field_provenance admin read-only (U5).

**Decided: folded into this phase as U0** (above). The open items from the PR #125 review, all minor: `write_answer` IntegrityError race on concurrent create; an expired row blocking lower-precedence writers; category reset on update; duplicated `_SOURCE_RANK`/`_is_blank` helpers in `answer_resolver.py` and `profile_fields.py`; per-question AnswerBank query inside `resolve_field_answers` (one query per question per draft); `UNCONFIRMED_ANSWERS` message wording; a missing downgrade test in `test_answer_resolver.py`. Also: confirm CI on `b77e825`, decide merge order of #125 vs #123, remove the `manualcheck` test user/job, update epic #117 with Phase 1 status. Not absorbed because `answers_schema_version` is read by nothing; left at 1.

## Existing tests that change on purpose
- `apps/web/tests/test_explicit_answers_views.py`: deleted; replaced by U9/U12 tests and a redirect test.
- `apps/web/tests/test_auth_and_profile.py::test_resume_upload_via_profile_form_routes_through_set_resume`: drop phone/linkedin assertions (moved to Answers tests).
- `apps/accounts/tests/test_field_provenance.py`: `ProfileFormProvenanceTests` phone/visa POSTs move to `/profile/answers/`; rematch assertions become 0 (Answers) / 1 (Profile); `test_one_save_means_one_rematch_trigger` phone → 0, target_tags → 1.
- `apps/accounts/tests/test_answer_resolver.py`: `NormalizeQuestionKeyTests` (options no longer in key), `ResolveAnswerTests` and `WriteAnswerPrecedenceTests` calls that pass `options=`, `LegacyFallbackTests` against `legacy:*` rows.
- `apps/accounts/tests/test_answerbank_models.py`: unique constraint now includes `scope_region`.
- `apps/auto_apply/tests/test_answer_resolution.py`: ExplicitAnswer-based classes re-seeded as `legacy:*` rows (ambiguous subtests keep assertions); `ProfileDerivedSalaryFallbackTests` replaced (min_salary never answers); `AnswerBankResolutionTests` legacy/profile-less cases re-seeded.
- `apps/auto_apply/tests/test_drafting_service.py`: `SalaryRegionOverrideProvenanceTests` rewritten on real `location_country`; legacy-seeded tests re-seeded.
- `apps/web/tests/test_auto_apply_views.py::LearnedAnswerEndToEndTests` (writes a bank row with `options=`) updated for the new key.
- `apps/accounts/tests/test_tiering.py` + corpus: new rows, v2.

## Risks
- Resolver/key refactor is a core-path change: characterization before/after U7; `_enforce_option_constraint` and the LLM batch untouched; conservative normalization (a false merge is worse than a miss).
- Value→option mapping could pick the wrong option: only exact casefold or unambiguous yes/no polarity; otherwise blank.
- Country/contact regexes answering wrongly: fail-closed (anchored, case-sensitive, single match), real dev labels as test inputs; doubt → `None`.
- Remember-on-review could lock in a wrong T0/T1 answer: opt-in only, locked rows shown with source and a delete button; location-sensitive answers are region-scoped.
- Typed facts override learned/user bank rows per the doc's precedence; provenance origin is visible in the queue.
- Legacy rows are country-agnostic (as in Phase 1); banner and per-country settings shrink this.
- Backfill deploy race (old code writing ExplicitAnswer after migrate): negligible for single-user dev; run migrate after code is live.
- Panel cache vs a concurrent build: bounded by the 1 h TTL.
- `tzdata` on the slim image; a 599-zone select is acceptable.
- Observation table grows with every send; index `(profile, question_key)`; retention policy deferred with GDPR (cascade on profile delete).

## Verification (end to end)
1. `DATABASE_URL=postgres://jobborg:jobborg@localhost:5432/jobborg DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test apps.accounts apps.auto_apply apps.web apps.matching apps.locations --noinput` green (the 7 known `responses`-module errors are env-only); `makemigrations --check` clean; `migrate` applies accounts 0011/0012 and auto_apply 0011 on the dev DB (clean worktree if the tree is polluted; never `git stash`).
2. Shell: user 5 has three locked `legacy:*` AnswerBank rows (`100-125k` → `$100,000 – $125,000`, region US); ExplicitAnswer admin read-only; no AnswerBank key contains `#`.
3. `/profile/explicit-answers/` redirects to `/profile/answers/`; `/profile/` shows search criteria only, saving redirects to recommendations and the worker logs `rematch_profile`.
4. On `/profile/answers/` set city/country/timezone, a USA `h1b` row and IND citizenship; save → same page + message, **no** `rematch_profile`, `field_provenance` has the new keys. With JS off, visa rows are shown and an unchanged save keeps them. Duplicate country → error with input kept.
5. Draft a US and an India job: "Country" = "India +91" (reason `profile_fact`); India "authorized to work in the country…" = "Yes"; US h1b sponsorship and "without restrictions" authorization blank + needs review; US salary shows the US band; a Japan or blank-country job's salary is blank + needs review; no crash on empty `location_country`.
6. **Answer memory:** in the queue, answer a T2 question (e.g. "How did you hear about us?") with Remember ticked and send; draft a different employer's job with the same question → prefilled from your answer. Answer a relocation question for a US job with Remember ticked; a draft for an India job leaves it blank + needs review. Answer a T0 question without "Always use" → not remembered; with it → locked row. Check `AnswerObservation` has one row per answered non-standard question per send.
7. Panel: "Country" row answered by settings; quick-fill an LLM-answered T0 question → error without the confirm box, locked row with `source_detail.draft_id` with it; the question then shows ✓.
8. Seed a learned T0 bank row and draft: still needs confirmation; Send is refused until confirmed (NFR2 unchanged).
