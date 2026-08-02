---
title: Candidate Retrieval Layer for Matching Fan-out
type: feat
status: active
date: 2026-07-20
---

# Candidate Retrieval Layer for Matching Fan-out

## Summary

Add a new `ProfileTag` join table that inverts `Profile.target_tags` (profile → tag, instead of tag → profile) and use it in `apps/matching/services.py`'s job-centric fan-out (`candidate_profiles_for_job`) to look up interested profiles by tag before the existing SQL pre-filter runs, instead of scanning every active profile. `ProfileTag` rows are kept in sync synchronously on every `Profile` save. Profiles with an empty `target_tags` list fall back to the existing full-scan path so they're never silently dropped. The profile-centric path (`rematch_profile_obj`) is untouched.

---

## Problem Frame

`apps/matching/services.py`'s `candidate_profiles_for_job` already applies a cheap SQL pre-filter (location, remote preference, excluded employers, salary) before scoring, which bounds *scoring* cost per job. But the pre-filter itself still queries every active `Profile` row — retrieval cost scales with total active-profile count, not with how many profiles are actually tag-relevant to a given job. At the project's current single-ATS, local-dev scale this is a non-issue, but the shape is worth designing now, documented ahead of need (see origin document — no build trigger is defined; this plan makes the design ready without implying urgency).

The codebase already has a GIN index on `Job.classification_tags` and an established versioned-denormalization/backfill pattern (`apps/locations`, from the recent location-matching work) that this plan draws on for migration and sync conventions, without needing that pattern's versioning machinery — `target_tags` has no computed/normalization step, just raw values, so there is no drift-over-time risk a version stamp would guard against.

A material consequence surfaced during planning: today, `match_job` scores every pre-filter-passing active profile, so a profile with zero tag overlap can still land `recommended` purely on title/location/salary signal. Making the tag index a real candidate gate necessarily excludes that profile from being scored at all for that job — there's no cheap, indexed way to predict a title/location/salary-only match without actually scoring. This is the standard recall trade-off every candidate-retrieval layer makes, and is accepted here as a deliberate decision (see Key Technical Decisions) rather than left implicit.

---

## Requirements

- R1. A new denormalized structure maps each tag to the set of profiles whose `target_tags` include it, kept in sync whenever a profile's `target_tags` changes.
- R2. Job-centric fan-out (`candidate_profiles_for_job`) retrieves candidate profiles by looking up each of the job's `classification_tags` in this structure and unioning the results, instead of querying all active profiles.
- R3. The existing cheap pre-filter (location, remote preference, excluded employers, salary) still applies to the retrieved candidate set before scoring — the tag structure narrows which profiles are queried; the pre-filter still narrows which of those pass.
- R4. A profile with an empty `target_tags` list is unreachable through the tag structure (no tag to key on) and must still be considered a candidate for every job, via an explicit fallback to the existing full pre-filter query for that subset.
- R5. This is documented and built as ready-to-use; it has no defined trigger metric or rollout timeline and takes effect as soon as it merges (unlike the origin document's "architecture-in-waiting" framing, there is no flag gating this — see Open Questions for why).

---

## Scope Boundaries

- Embedding generation at ingestion time and vector-similarity candidate retrieval — out of scope, a separate future item per the origin document.
- Ranking on the existing GIN-indexed `classification_tags` field without a new table — considered and not chosen; the origin document notes it as a fallback direction if scoring cost (not query cost) turns out to be the bottleneck.
- The profile-centric rematch path (`rematch_profile_obj` / `apps/matching/tasks.py`'s `rematch_profile`) — unaffected; it already scores one profile against a bounded recent-job window, not a fan-out to many profiles.
- A version-stamp field on the new tag structure — not added; `target_tags` has no computed/interpretation layer today, so there is nothing that could go stale independent of the raw field itself. If tag interpretation logic (normalization, synonyms) is added later, a version field can be introduced at that point.
- A top-N candidate cap on top of the tag lookup — not added; the tag lookup plus existing pre-filter is assumed sufficient to bound the set for now.
- Profiles with zero tag overlap on a job no longer receive a `UserJobMatch` row for that job, even one scored `below_threshold` — an accepted, deliberate behavior change (see Problem Frame, Key Technical Decisions).

### Deferred to Follow-Up Work

- A periodic sweep/rebuild task for the tag structure — not needed today since sync is synchronous and immediate on every `Profile` save; would only become relevant if the sync mechanism were later changed to something eventually-consistent.
- Admin visibility into the new `ProfileTag` table (e.g. a `list_filter` or inline) — not requested, add if debugging need arises.

---

## Context & Research

### Relevant Code and Patterns

- `apps/matching/services.py` (`candidate_profiles_for_job`, `profile_snapshot`, `job_snapshot`, `match_job`) — the function this plan modifies; already deletes stale `UserJobMatch` rows for profiles that fall out of the candidate set (`match_job`'s "Drop stale matches ... for users who are no longer candidates" step), so no new stale-row cleanup logic is needed — the existing exclude-then-delete flow naturally handles profiles the tag structure no longer surfaces.
- `apps/matching/models.py` (`UserJobMatch`) — the join-table shape (`FK + FK`, `UniqueConstraint`, targeted `models.Index`) this plan's new `ProfileTag` model mirrors.
- `apps/matching/signals.py` (`rematch_on_profile_save`, `post_save` on `Profile`, `dispatch_uid="profile_rematch"`) — the existing signal hook; this plan adds a second receiver on the same signal, not a modification to the existing one, so the debounced-rematch behavior is untouched.
- `apps/matching/tests/factories.py` (`make_profile`, `make_job`) — plain-function factories (not `factory_boy`); existing tests mock `apps.matching.signals.schedule_rematch` in `setUp` to suppress the real debounced rematch enqueue during profile creation — the new tag-sync receiver is a separate function and is deliberately **not** mocked, since tests need real `ProfileTag` rows to exist for retrieval assertions.
- `apps/jobs/models.py` (`classification_tags` `JSONField`, `GinIndex(fields=["classification_tags"], name="job_tags_gin")`) — the existing indexing convention for tag-shaped `JSONField`s; `Profile.target_tags` is the equivalent JSONField on the profile side (`apps/accounts/models.py`).
- `apps/accounts/migrations/0002_profile_target_locations_alias_version_and_more.py` → `0003_..._index.py` → `0004_backfill_profile_locations.py` — the established split of schema-add, index-add, and `RunPython` backfill into separate migrations; this plan's `ProfileTag` migrations follow the same split (schema, then backfill — no separate index migration needed since the index is declared on the new model at creation).
- `apps/locations/services.py` — the version-guarded conditional-update backfill pattern; not reused verbatim here (no version field), but its core idea — writes must not clobber concurrent signal-driven writes — still applies and is addressed via `bulk_create(..., ignore_conflicts=True)` rather than a version-guarded update, since a `ProfileTag` row's existence (not its content) is the only thing that can conflict.
- `apps/matching/tests/test_fanout_integration.py`, `apps/matching/tests/test_match_job_to_profiles.py` — existing tests exercise `match_job`/`candidate_profiles_for_job` assuming every active profile is a candidate regardless of tags; these need review once `candidate_profiles_for_job` starts tag-narrowing, since some existing test profiles/jobs may not share tags and would need `target_tags`/`classification_tags` added to their factory calls to remain valid candidates under the new behavior.

### Institutional Learnings

- `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md` — a past matching-scoring bug where a "fallback" branch's logic was subtly wrong for an edge case (unresolved profile target vs. resolved job). Its explicit lesson — "treat 'unresolved'/fallback as a first-class state with its own named test, not a broad catch-all" and "get adversarial review on any safety-net fallback logic" — applies directly to this plan's untagged-profile fallback (R4) and is reflected in U4's test scenarios below.
- `apps/locations` (from the 2026-07-19 location-matching plan) is the concrete precedent for "denormalized structure feeding the matching pipeline, kept in sync via signals/migrations" — read for convention, not reused for its versioning machinery (see Scope Boundaries).

### External References

- None — local patterns are sufficient; no external research was performed (see planning dialogue).

---

## Key Technical Decisions

- **Join-table model (`ProfileTag`), not a JSONField/array on `Profile`**: mirrors the existing `UserJobMatch` precedent most closely and is the simplest shape for the per-tag union-lookup query (`ProfileTag.objects.filter(tag__in=job.classification_tags)`), versus needing an `overlap`/`contains`-style JSONField query across all profiles for the same result.
- **Synchronous full reconciliation on every `Profile` save, not a diff or a debounced async rebuild**: `target_tags` lists are small, so reconciling (create-missing, delete-removed) inline in the `post_save` signal is cheap and keeps `ProfileTag` immediately consistent — no eventual-consistency window, no need for the debounce infrastructure `schedule_rematch` uses (that exists because *rematching* is expensive; tag-sync is not).
- **No version-stamp field**: unlike `apps/locations`'s `location_alias_version`, there is no computed/normalization step between `Profile.target_tags` and `ProfileTag` rows — they are a direct reflection of raw data, so there is nothing that can go stale independent of `target_tags` itself changing (which the signal already catches). Revisit only if tag interpretation logic is introduced later.
- **Tag overlap becomes a real candidacy gate, accepting a recall trade-off**: resolved explicitly during planning (see Problem Frame). A profile with non-empty `target_tags` that don't overlap a job's `classification_tags` is no longer scored for that job at all, even though today it could still land `recommended` via title/location/salary alone. This is deliberate — it's the only way this layer actually reduces per-job query cost for tagged profiles, and is the standard trade-off inherent to any candidate-retrieval design. Untagged profiles (R4) are explicitly exempted via the full-scan fallback, since they have stated no tag preference at all.
- **Untagged-job case needs no special handling**: a job with empty `classification_tags` naturally retrieves zero tag-matched profiles (nothing can overlap an empty list) plus the untagged-profile fallback — this falls out of the existing query shape without extra logic, and is covered by an explicit test (U4) so the behavior is verified, not assumed.

---

## Open Questions

### Resolved During Planning

- Whether to accept the zero-tag-overlap recall trade-off or make the tag structure a soft signal only: resolved to accept the trade-off — see Key Technical Decisions and Problem Frame. A "soft narrowing, never excludes" version was considered and rejected because it cannot actually reduce query cost for tagged profiles without an equally cheap indexed proxy for title/location/salary-driven matches, which doesn't exist.
- Whether `ProfileTag` needs a version-stamp field like `apps/locations`'s alias versioning: no — there is no computed/interpretation layer over `target_tags` today (see Key Technical Decisions).
- Whether a periodic sweep/rebuild task is needed: no — sync is synchronous and immediate, so there's no eventually-consistent window a sweep would need to close (see Scope Boundaries, Deferred to Follow-Up Work).

### Deferred to Implementation

- Whether the union-lookup query (`ProfileTag.objects.filter(tag__in=...)` combined with the untagged-profile fallback) needs a combined `Q`/`distinct()` queryset or two separate ID-set queries unioned in Python — an implementation-time choice with no product-visible difference; either is fine as long as `is_active` filtering and the existing excludes still apply once, not twice, to the combined set.
- Exact `ProfileTag.tag` field length/type — should match whatever `Job.classification_tags`' individual tag strings look like in practice (checked during implementation, not assumed here).

---

## Implementation Units

### U1. `ProfileTag` model (schema)

**Goal:** A new join-table model mapping each profile to each of its target tags, ready to be queried by tag.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Modify: `apps/matching/models.py` (add `ProfileTag`)
- Create: `apps/matching/migrations/0002_profiletag.py` (schema-only, auto-generated)

**Approach:**
- `ProfileTag`: `profile` FK to `accounts.Profile` (`on_delete=models.CASCADE`, `related_name="tags"`), `tag` `CharField` (length matched to how classification tags are actually shaped — check `apps/classification` for the convention used there before picking a length).
- `UniqueConstraint(fields=["profile", "tag"])` — a profile can't have the same tag twice.
- `models.Index(fields=["tag"])` for the union-lookup query in U4 (`tag__in=[...]`); the FK's implicit index already covers profile-side lookups (e.g. "all tags for this profile," used by U2's reconciliation).

**Patterns to follow:**
- `apps/matching/models.py`'s `UserJobMatch` — FK + `UniqueConstraint` + targeted `models.Index` shape.

**Test scenarios:**
- Test expectation: none — pure schema addition, no behavioral change. Verified by running the migration cleanly against the existing test DB.

**Verification:**
- Migration applies cleanly; a `ProfileTag` row can be created and enforces the `(profile, tag)` uniqueness constraint (duplicate raises `IntegrityError`).

---

### U2. Signal-based reconciliation on `Profile` save

**Goal:** `ProfileTag` rows for a profile always exactly reflect that profile's current `target_tags`, immediately after every save.

**Requirements:** R1

**Dependencies:** U1

**Files:**
- Modify: `apps/matching/signals.py` (add a second `post_save` receiver on `Profile`, e.g. `sync_profile_tags`, alongside the existing `rematch_on_profile_save`)
- Create: `apps/matching/tests/test_signals.py` (or extend an existing signals-adjacent test file if one better fits)

**Approach:**
- On every `Profile` post-save: compute `new_tags = set(instance.target_tags)`, compare against `existing_tags = set(ProfileTag.objects.filter(profile=instance).values_list("tag", flat=True))`. `bulk_create` a `ProfileTag` for each tag in `new_tags - existing_tags`; `.delete()` the rows for `existing_tags - new_tags`. No diffing of the *previous* `target_tags` value is needed — reconciling against current DB state is simpler and equally correct.
- Registered as a second `@receiver(post_save, sender=Profile, dispatch_uid="profile_tag_sync")` in the same file, not a modification of the existing `rematch_on_profile_save` — keeps the debounced-rematch concern and the tag-sync concern independent and separately testable.
- Runs synchronously inline in the save path (no Celery task, no debounce) — this is a cheap set-diff over a short list, unlike the rematch computation `schedule_rematch` exists to debounce.

**Patterns to follow:**
- `apps/matching/signals.py`'s existing `@receiver(post_save, sender=Profile, dispatch_uid=...)` structure.

**Test scenarios:**
- Happy path: creating a `Profile` with `target_tags=["python", "django"]` results in exactly two `ProfileTag` rows for that profile.
- Edge case: updating `target_tags` from `["python"]` to `["python", "django"]` adds one new `ProfileTag` row, leaves the existing `"python"` row untouched (not deleted and recreated).
- Edge case: updating `target_tags` from `["python", "django"]` to `["python"]` removes exactly the `"django"` row.
- Edge case: clearing `target_tags` to `[]` removes all `ProfileTag` rows for that profile.
- Edge case: duplicate tags in `target_tags` (e.g. `["python", "python"]`) result in exactly one `ProfileTag` row (dedup via the set-based diff).
- Edge case: saving a `Profile` with unchanged `target_tags` (e.g. an unrelated field edit) leaves `ProfileTag` rows untouched — no unnecessary delete/recreate churn.
- Integration: saving a `Profile` through the normal save path (not a raw signal test) produces correct `ProfileTag` rows with no Celery task involved — this is synchronous, unlike `schedule_rematch`.

**Verification:**
- `set(ProfileTag.objects.filter(profile=p).values_list("tag", flat=True))` equals `set(p.target_tags)` immediately after any `Profile.save()`, in every test scenario above.

---

### U3. Backfill migration for existing profiles

**Goal:** `Profile` rows that existed before this feature get their `ProfileTag` rows populated, without waiting for their next save.

**Requirements:** R1

**Dependencies:** U1, U2

**Files:**
- Create: `apps/matching/migrations/0003_backfill_profile_tags.py` (`RunPython`, resolving `Profile`/`ProfileTag` via the migration's historical `apps.get_model(...)`, with a no-op reverse)

**Approach:**
- For every existing `Profile` with a non-empty `target_tags`, `bulk_create` the corresponding `ProfileTag` rows with `ignore_conflicts=True` — safe to run more than once (a row already created by U2's signal, e.g. if a profile happens to be saved between deploy and migration running, is simply skipped as a conflict, not duplicated or errored on).
- Profiles with empty `target_tags` need no rows created — nothing to backfill for them; they're covered by the fallback query at retrieval time (U4), not by `ProfileTag` membership.
- No batching infrastructure is added — the existing profile table is small enough that a single bounded query (`Profile.objects.exclude(target_tags=[])`) followed by one `bulk_create` is sufficient; revisit only if profile count grows large enough to matter.

**Patterns to follow:**
- `apps/accounts/migrations/0004_backfill_profile_locations.py`'s `RunPython` + historical-model-resolution structure (though this backfill is simpler — no version-guarded conditional update needed, since `ignore_conflicts=True` is sufficient here: existence, not content, is what could conflict).

**Test scenarios:**
- Happy path: running the backfill against a set of pre-existing `Profile` rows with varied `target_tags` produces exactly the correct `ProfileTag` rows for each.
- Edge case: a profile with empty `target_tags` produces no `ProfileTag` rows.
- Edge case: running the backfill twice in a row is a no-op the second time — no duplicate rows, no `IntegrityError`.
- Edge case: a profile whose `ProfileTag` rows were already created by U2's signal (e.g. saved after deploy but before migration runs) is left untouched — the backfill's `bulk_create` for that profile's tags conflicts harmlessly and is skipped.

**Verification:**
- After the migration runs, every pre-existing `Profile` with non-empty `target_tags` has `ProfileTag` rows exactly matching its tag set; re-running the migration's underlying logic is a safe no-op.

---

### U4. Job-centric retrieval integration

**Goal:** `candidate_profiles_for_job` narrows candidates via the tag structure (plus the untagged-profile fallback) before the existing pre-filter runs, instead of scanning every active profile.

**Requirements:** R2, R3, R4, R5

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `apps/matching/services.py` (`candidate_profiles_for_job`)
- Modify: `apps/matching/tests/factories.py` if needed (ensure `make_profile`/`make_job` can express overlapping/non-overlapping tags conveniently for the new test scenarios)
- Modify: `apps/matching/tests/test_match_job_to_profiles.py` and/or `apps/matching/tests/test_fanout_integration.py` (new and updated cases — review existing cases that assumed every active profile is a candidate regardless of tags, and give them overlapping `target_tags`/`classification_tags` where the test's intent requires the profile to remain a candidate)

**Technical design:**

> This illustrates the intended approach and is directional guidance for review, not implementation specification.

```
candidate_profiles_for_job(job):
    tagged_ids = ProfileTag.objects.filter(tag__in=job.classification_tags)
                     .values_list("profile_id", flat=True)
    untagged_ids = Profile.objects.filter(target_tags=[])
                     .values_list("id", flat=True)
    candidate_ids = set(tagged_ids) | set(untagged_ids)

    qs = Profile.objects.filter(id__in=candidate_ids, is_active=True)...
    # ...existing remote-pref / excluded-employer / salary excludes unchanged, applied after
```

**Approach:**
- The tag-structure lookup and the untagged-profile fallback are combined into one candidate ID set *before* the existing `is_active`/remote-pref/excluded-employer/salary excludes run — those excludes are unchanged, just now applied to a narrower starting queryset instead of `Profile.objects.filter(is_active=True)` directly.
- A job with empty `classification_tags` naturally yields zero `tagged_ids` (nothing can overlap an empty list); only the untagged-profile fallback contributes candidates for such a job — no special-case branch needed, but explicitly tested (see below) so the behavior is verified rather than assumed.
- `match_job`'s existing "drop stale matches for users who are no longer candidates" step (in `apps/matching/services.py`) needs no changes — it already deletes `UserJobMatch` rows for any profile the (now-narrower) candidate set doesn't include, which is exactly the desired behavior for profiles that fall out of tag relevance.

**Patterns to follow:**
- The existing `candidate_profiles_for_job` structure (queryset built incrementally via `.exclude()` calls) — extend the front of it, don't restructure the excludes that already work.

**Test scenarios:**
- Happy path: job with `classification_tags=["python"]`; profile A has `target_tags=["python"]`, profile B has `target_tags=["java"]` → only A appears in `candidate_profiles_for_job(job)`.
- Happy path (R4, untagged fallback): profile C has `target_tags=[]` → C is a candidate for every job regardless of tag overlap, subject to the existing excludes.
- Edge case: job with `classification_tags=[]` (untagged job) → only untagged profiles (via the R4 fallback) are candidates; a profile with non-empty, non-overlapping `target_tags` is excluded.
- Edge case: a tag-matched profile is still excluded by the existing pre-filter (e.g. `excluded_employers` contains this job's employer) — confirms the pre-filter still fully applies after tag-narrowing, not bypassed.
- Edge case (recall trade-off, explicit regression coverage per the "no silent fallback" lesson from `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md`): a profile with non-empty `target_tags` that do **not** overlap a job's `classification_tags` is excluded from candidates entirely, even when other scoring signals (title/location/salary) would otherwise have produced a `recommended` match — confirms the accepted trade-off behaves as decided, not as an accidental side effect.
- Integration: `match_job(job)` end-to-end — a profile that previously had a `below_threshold` `UserJobMatch` row purely from non-tag signals, with zero tag overlap, has that row deleted on the next `match_job` run for that job (via the existing stale-match cleanup), and no new row is created for it going forward.
- Integration: `match_job(job)` end-to-end for an untagged profile — it still receives a scored `UserJobMatch` row (at whatever score/status the scorer produces), confirming R4's fallback reaches all the way through to a real match row, not just candidate-set membership.

**Verification:**
- `candidate_profiles_for_job(job)` never includes a profile with non-empty, non-overlapping `target_tags` (except via the existing excludes, which are unrelated to tags).
- `candidate_profiles_for_job(job)` always includes every active, otherwise-eligible untagged profile, for every job regardless of that job's tags.
- Existing `apps/matching/tests/test_fanout_integration.py`/`test_match_job_to_profiles.py` cases pass with tags added to factory calls where the test's intent requires a profile to remain a candidate.

---

## System-Wide Impact

- **Interaction graph:** `apps/matching/services.py`'s `candidate_profiles_for_job` gains a dependency on the new `ProfileTag` model; `apps/matching/signals.py` gains a second `Profile` post-save receiver, independent of the existing rematch-debounce receiver.
- **Error propagation:** no new exception paths — the reconciliation logic (U2) and retrieval query (U4) are plain queryset operations with no new failure modes beyond what `Profile`/`ProfileTag` saves already have.
- **State lifecycle risks:** the backfill migration (U3) is the primary one-time risk surface, mitigated by `ignore_conflicts=True` making it safe to run alongside already-deployed signal-driven writes (U2).
- **Behavioral change (product-visible in aggregate, not to end users directly):** profiles with zero tag overlap on a job no longer receive even a `below_threshold` `UserJobMatch` row for it. Per the existing V1 design, only `match_status="recommended"` rows are ever surfaced to users, so this has no direct end-user visibility impact — but it does reduce the total `UserJobMatch` row count and would affect any future analytics built directly on `below_threshold` rows.
- **Test suite impact:** existing tests that assume every active profile is a match-job candidate regardless of tags (in `apps/matching/tests/test_fanout_integration.py`, `test_match_job_to_profiles.py`) need review under U4 — some may need `target_tags`/`classification_tags` added to factory calls to keep testing what they originally intended to test.
- **Unchanged invariants:** `apps/matching/prefilter.py` and `rematch_profile_obj`'s dict-based prefilter path are untouched — only the SQL-level `candidate_profiles_for_job` changes. `MATCH_SCORE_THRESHOLD` and the scoring weights in `apps/matching/constants.py` are unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Existing tests implicitly rely on full-scan candidacy (every active profile is a candidate regardless of tags) and could start failing once `candidate_profiles_for_job` tag-narrows | U4 explicitly reviews and updates `test_fanout_integration.py`/`test_match_job_to_profiles.py` as part of the unit, not as an afterthought |
| The accepted recall trade-off (zero-tag-overlap profiles never scored) could surprise a future reader who doesn't know it was a deliberate choice | Documented explicitly in Problem Frame, Key Technical Decisions, Scope Boundaries, and as a named test scenario in U4 — not left implicit |
| Backfill migration (U3) running concurrently with live `Profile` saves (U2 already deployed) could theoretically race | `ignore_conflicts=True` makes duplicate-row attempts a no-op rather than an error; no lost-update risk exists here since `ProfileTag` rows have no mutable content to clobber (a row either exists or doesn't) |

---

## Documentation / Operational Notes

- Good `/ce-compound` candidate once shipped: the "candidate-retrieval layer accepts a recall trade-off" pattern, and the "no version field needed when there's no computed/interpretation layer" reasoning, are both reusable lessons — the `docs/solutions/` entry on the `ONSITE_ONLY` fallback bug is the closest existing precedent for how to write this up.

---

## Sources & References

- Origin document: `docs/brainstorms/2026-07-19-candidate-retrieval-layer-requirements.md`
- Related code: `apps/matching/services.py`, `apps/matching/models.py`, `apps/matching/signals.py`, `apps/matching/tasks.py`, `apps/matching/prefilter.py`, `apps/matching/tests/factories.py`, `apps/accounts/models.py`, `apps/jobs/models.py`, `apps/locations/services.py` (versioning-pattern precedent, not reused), `apps/accounts/migrations/0004_backfill_profile_locations.py` (backfill-migration-shape precedent).
- Related plan: `docs/plans/2026-07-19-001-feat-location-matching-plan.md` (closest-precedent plan structure and denormalization/backfill conventions).
- Related learning: `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md` (fallback-safety-net review lesson, applied to R4's untagged-profile handling).
