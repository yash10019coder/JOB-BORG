---
title: ONSITE_ONLY Location Filtering Ignored target_locations (Substring Matching Caused False Positives)
date: 2026-07-19
category: logic-errors
module: apps.matching.scoring
problem_type: logic_error
component: service_object
symptoms:
  - "ONSITE_ONLY profiles receive full location credit for any onsite job regardless of target_locations"
  - "Location filters silently no-op for onsite-only candidates while appearing to work for ANY remote_pref"
  - "Raw substring location matching produces false positives (e.g. target 'NY' matches job location 'Albany')"
  - "Abbreviated location targets (e.g. 'NY') never match full location names (e.g. 'New York')"
  - "Substring fallback still false-positive-matched unresolved profile targets against structurally-resolved job locations after the first fix attempt"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [django, location-matching, scoring-engine, substring-matching-bug, code-review-catch, remote-pref, data-migration]
---

# ONSITE_ONLY Location Filtering Ignored target_locations (Substring Matching Caused False Positives)

## Problem

`apps/matching/scoring.py`'s `_location_component()` gave `ONSITE_ONLY` profiles full location credit for *any* onsite job, silently ignoring `target_locations` entirely — a user who set both `ONSITE_ONLY` and a specific target city/region/country would get matched to onsite jobs anywhere in the world. Separately, the `ANY`-preference branch that did check location used a raw lowercase-substring comparison (`t in location`), which broke on abbreviations ("NY" vs. "New York"), produced false positives ("ny" substring-matching inside "Albany"), and had no concept of city/region/country hierarchy (a target of "California" wouldn't match a job listed as "San Francisco, CA"). User-visible impact: users reported "location filters aren't working" — onsite-only users saw irrelevant out-of-area jobs, and ANY-preference users saw both false positives (wrong city sharing a substring) and false negatives (no hierarchy awareness).

## Symptoms

- `ONSITE_ONLY` profiles matched onsite jobs regardless of `target_locations` — the field was read into the profile snapshot but never consulted in that branch.
- Location substring matching produced false positives: a profile targeting "NY" would match a job located in "Albany, NY, US" purely because `"ny"` is a substring of `"albany"`.
- No hierarchy awareness: a profile targeting "California" (region-only) would not match a job whose location string was just "San Francisco, CA" unless the literal target string was a substring of the job's raw location text.
- Root user report: "location filters aren't working."

## What Didn't Work

The original scorer conflated remote-preference gating with location matching and simply skipped location checks for `ONSITE_ONLY`:

```python
def _location_component(profile, job):
    remote_pref = profile.get("remote_pref", Profile.RemotePref.ANY)
    is_remote = bool(job.get("is_remote"))
    if remote_pref == Profile.RemotePref.REMOTE_ONLY:
        return 1.0 if is_remote else 0.0
    if remote_pref == Profile.RemotePref.ONSITE_ONLY:
        return 0.0 if is_remote else 1.0   # BUG: ignored target_locations entirely
    if is_remote:
        return 1.0
    targets = [loc.lower() for loc in (profile.get("target_locations") or [])]
    if not targets:
        return 1.0
    location = (job.get("location") or "").lower()
    return 1.0 if any(t and t in location for t in targets) else 0.0
```

During implementation of the structured fix, the first version of `_match_targets`'s substring-fallback trigger fired whenever **either side** — the job or the profile's target — was unresolved. That reintroduced the exact "ny"-in-"albany" false positive class, just for a differently-shaped input: a bare unresolved target (e.g. `"NY"`, correctly marked `resolved=False` by `apps.locations`'s disambiguation rules since a standalone state abbreviation is ambiguous) compared via raw substring against a job whose location *was* structurally resolved to `Albany, NY, US`. Adversarial code review caught this and the condition had to be tightened to fall back to substring matching only when the **job's** location is unresolved — an unresolved profile target against a resolved job now correctly yields no match, not a substring guess. This is covered by the regression test `test_bare_abbreviation_target_does_not_substring_match_resolved_job`.

A related near-miss surfaced in `apps/locations/engine.py`'s `_resolve_segments`: the initial multi-segment resolution logic fell back to an unconstrained head-only city match when the tail segment (e.g. the region/country part) didn't resolve to anything curated — so `"Austin, Georgia"` would confidently resolve to Austin, TX, US, silently discarding the unrecognized "Georgia" tail. This is the same "confidently wrong" bug class the whole dataset exists to prevent, just at the normalization layer instead of the scoring layer. Fixed to return fully unresolved when the tail is unrecognized, covered by `test_unrecognized_tail_does_not_fall_back_to_unconstrained_city`.

## Solution

Two-part fix, both in the current code.

**1. `_location_component` now routes `ONSITE_ONLY` through the same target-location check as `ANY`:**

```python
def _location_component(profile, job):
    remote_pref = profile.get("remote_pref", Profile.RemotePref.ANY)
    is_remote = bool(job.get("is_remote"))

    if remote_pref == Profile.RemotePref.REMOTE_ONLY:
        return 1.0 if is_remote else 0.0
    if remote_pref == Profile.RemotePref.ONSITE_ONLY:
        if is_remote:
            return 0.0
        return _match_targets(profile, job)

    # ANY: remote satisfies location wholesale; otherwise check target locations.
    if is_remote:
        return 1.0
    return _match_targets(profile, job)
```

**2. Location matching moved from substring comparison to structured hierarchy matching**, with a bounded substring fallback reserved for the genuine thin-coverage case:

```python
def _hierarchy_match(target, job):
    if target["city"] and target["city"] != job.get("location_city"):
        return False
    if target["region"] and target["region"] != job.get("location_region"):
        return False
    if target["country"] and target["country"] != job.get("location_country"):
        return False
    return True

def _match_targets(profile, job):
    targets = profile.get("target_locations_normalized") or []
    if not targets:
        return 1.0  # no location constraint stated

    if any(_hierarchy_match(t, job) for t in targets if t["resolved"]):
        return 1.0

    if job.get("location_resolved"):
        # Job is structured; an unresolved target must NOT substring-fallback
        # against it -- that's the "ny" in "albany" bug again.
        return 0.0

    # Job itself isn't curated yet -- fall back to substring rather than
    # penalizing users for a coverage gap outside their control.
    return _substring_fallback(profile.get("target_locations"), job.get("location"))
```

Underpinning this is `apps/locations/engine.py`'s `normalize_location`, which parses free text into `{city, region, country, resolved}` using a curated, versioned YAML alias dataset (mirroring `apps/classification`). `_resolve_segments` treats an unset target level as a wildcard (region-only targets match any city in that region) and, critically, refuses to resolve a multi-segment input when the tail segment is unrecognized rather than guessing from the head alone:

```python
if country is None and region is None:
    # An unrecognized tail means the whole entry stays unresolved, not
    # "trust the city alone."
    return dict(_UNRESOLVED)
```

Both fixes are exercised in `apps/matching/tests/test_scoring.py::LocationComponentTests` (notably `test_onsite_only_now_honors_target_locations` and `test_bare_abbreviation_target_does_not_substring_match_resolved_job`) and `apps/locations/tests/test_engine.py` (`test_unrecognized_tail_does_not_fall_back_to_unconstrained_city`).

## Why This Works

The root cause was two independent shortcuts taken by "quick" comparison logic: (1) the `ONSITE_ONLY` branch was written as if remote-preference and location-targeting were mutually exclusive concerns, when a user can legitimately want both "not remote" and "in this specific place" — the fix simply routes both onsite-eligible branches (`ANY` non-remote and `ONSITE_ONLY` non-remote) through the same `_match_targets` logic. (2) Raw substring comparison on free-text location strings has no way to distinguish "NY" as a meaningful token from "NY" as an accidental substring of "Albany" — there's no structure to reason about containment vs. coincidence. Replacing it with field-level hierarchy comparison (`city`/`region`/`country` compared independently, with unset levels as wildcards) eliminates the coincidental-substring class of false positive entirely, because matching now requires the *normalized structured representation* to agree, not the raw text.

The fallback-scope tightening (job-unresolved only, not either-side-unresolved) works because the asymmetry matters: if the *job* is unresolved, there's no structured data to compare against at all, so substring is the only signal available and was always the pre-fix behavior — no regression. But if the *job* is resolved and only the *target* is unresolved, structured data exists and disagrees implicitly (nothing in the target hierarchy matched it) — falling back to substring at that point isn't "graceful degradation for missing data," it's silently overriding a structured "no match" with a text coincidence.

The engine-level fix (unrecognized tail returns unresolved) works for the analogous reason at the normalization layer: with `len(segments) >= 2`, the tail is the disambiguating signal (region/country). Discarding an unrecognized tail and keeping only the head silently converts "I don't understand this input" into "I confidently understood this input as something else," which is worse than admitting non-resolution — the correct downstream behavior (raw substring fallback) can still find a legitimate match on the full string, whereas a wrong structured resolution poisons the hierarchy comparison with false confidence.

## Prevention

- **Test the exact bug shape as a named regression, not just the general feature.** Both `test_bare_abbreviation_target_does_not_substring_match_resolved_job` and `test_unrecognized_tail_does_not_fall_back_to_unconstrained_city` exist specifically because the *first* attempted fix reintroduced the original bug in a new shape. Comment blocks on these tests explicitly reference "Adversarial-review regression" — keep that pattern: when a review catches a near-miss reintroduction, encode it as a permanent test with a comment naming what it guards against, not just a fixed line of code.
- **Treat "unresolved" as a first-class state, not an error to paper over.** Both `_match_targets` and `_resolve_segments` had a tempting shortcut available (substring-fallback broadly; head-only city match) that "worked" for the common case but reintroduced confidently-wrong matches on the edge case. The fix in both places was to make the unresolved/no-match path an explicit, narrow branch rather than a broad catch-all — resist widening a fallback's trigger condition without a specific test proving it can't reintroduce the original failure mode.
- **Get adversarial code review on any "safety net" fallback logic before it ships**, especially where two similarly-named but differently-scoped states exist (job-unresolved vs. target-unresolved; tail-unresolved vs. whole-input-unresolved) — this class of bug hides in exactly that kind of subtle scope confusion, and it took a dedicated adversarial review pass to catch both instances here.
- **Keep the dependency direction and versioning discipline that made this fix safe to backfill**: `apps/locations` stays a dependency-free leaf app (never imports `jobs`/`accounts`/`matching`) so normalization logic can be reused across ingestion and profile-save without circular imports; `CURRENT_LOCATION_ALIAS_VERSION` gates re-normalization so a future curated-dataset or logic change has a clear signal for which rows are stale; the backfill migration uses version-guarded conditional updates (not blind `.save()`/bulk writes) so it's safe to interleave with concurrent ingestion/profile-save traffic. Substring fallback is intentionally retained (not replaced with a hard no-match) because the curated dataset's real-world coverage is still partial post-backfill (44.5% of real jobs resolved structurally after the initial backfill) — removing the fallback prematurely would silently zero out location scoring for the uncurated majority rather than gracefully degrading to pre-fix behavior for them.

## Related Issues

None found — this is the first documented solution in this repo's `docs/solutions/` knowledge store.
