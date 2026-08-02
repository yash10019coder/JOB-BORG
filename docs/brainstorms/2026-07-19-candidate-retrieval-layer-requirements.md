---
date: 2026-07-19
topic: candidate-retrieval-layer
---

# Candidate Retrieval Layer for Matching Fan-out

## Summary

Add a candidate-retrieval layer to the matching fan-out: a precomputed profile→tag inverted index that job-centric fan-out queries by tag instead of scanning every active profile. Documented as a ready-to-build V2 design with no defined trigger — it does not change V1 behavior until a decision is made to build it.

---

## Problem Frame

V1's matching fan-out (`apps/matching`) already applies a cheap SQL pre-filter (location, remote preference, excluded employers, salary — `apps/matching/prefilter.py`) before scoring, and this bounds the *scoring* cost per job. But the pre-filter itself still runs against every active profile: retrieval cost scales with total active-profile count, not with how many profiles are actually relevant to a given job's tags.

At small scale (the project's current single-ATS, local-dev footprint) this is a non-issue. At the scale the design should anticipate — large job and user volumes — a single job can still leave a large candidate set after pre-filtering purely on broad criteria like location and remote preference, before any tag relevance is considered. Reasoning about that shape now, while the fan-out entry points (`match_job_to_profiles`, `rematch_profile`) are still small and well-understood, is cheaper than retrofitting it later.

The codebase already has two pieces of infrastructure that anticipate this direction: a GIN index on `Job.classification_tags` and a nullable pgvector `embedding` column on `Job` (unused — no embedding generation exists yet). This design uses the tag infrastructure that already exists; it does not depend on or extend the embedding column.

---

## Requirements

**Inverted index structure**
- R1. A new denormalized structure maps each tag to the set of active profiles whose `target_tags` include it, kept in sync whenever a profile's `target_tags` change.
- R2. Job-centric fan-out retrieves candidate profiles by looking up each of a job's `classification_tags` in the inverted index and unioning the results, instead of scanning all active profiles.
- R3. The existing cheap pre-filter (location, remote preference, excluded employers, salary) still applies to the retrieved candidate set before scoring — the inverted index narrows *which profiles are queried*, it does not replace the pre-filter's job of narrowing *which of those pass*.

**Fallback for untagged profiles**
- R4. A profile with an empty `target_tags` list has no tag to key on and is unreachable through the inverted index. Such profiles must still receive recommendations: route them through the existing full pre-filter scan as a fallback, rather than silently dropping them from job-centric fan-out.

**Scope and rollout**
- R5. This design has no defined trigger metric or rollout timeline. It is documented as ready-to-build and does not modify or replace the existing V1 fan-out unless and until a separate decision is made to implement it.

---

## Acceptance Examples

- AE1. **Covers R4.** Given a profile with `target_tags = []`, when job-centric fan-out runs for any job, the profile is still evaluated via the existing full pre-filter path and can still receive a `UserJobMatch` row if it passes.

---

## Success Criteria

- Job-centric fan-out's retrieval step scales with the number of profiles interested in a job's tags, not with total active-profile count.
- Profiles with empty `target_tags` see no regression in recommendation coverage versus current V1 behavior.
- `ce-plan` can design the sync mechanism, index shape, and rollout without having to invent the fallback behavior or re-litigate why this approach was chosen over the alternatives.

---

## Scope Boundaries

- Embedding generation at ingestion time and vector-similarity candidate retrieval — deferred as a separate future item, not designed here.
- Ranking on the existing GIN-indexed `classification_tags` field (without a new inverted-index table) — considered and not chosen; noted as a fallback direction if scoring cost, rather than query/scan cost, turns out to be the actual bottleneck.
- A specific trigger condition (metric or threshold) for when this gets built — intentionally left undefined; this is architecture documented ahead of need.
- The profile-centric rematch path (`rematch_profile`) — it already scores one profile against a bounded recent-job window rather than fanning out to many profiles, so it isn't affected by this retrieval layer.

---

## Key Decisions

- **Precomputed profile→tag inverted index, not ranking on the existing GIN index**: only the inverted-index approach changes the retrieval query's shape (no scan of every active profile); ranking on the existing index would only trim the scoring pass after that scan already happened.
- **No dedicated search engine (e.g. full-text `tsvector`/`ts_rank`)**: tags are discrete, exact-match values, not free text needing fuzzy relevance scoring — a dedicated search index would add infrastructure without solving a need this doc has.
- **Untagged profiles fall back to the existing full-scan pre-filter path, not excluded from job-centric fan-out**: preserves current V1 behavior for that subset rather than introducing a regression.

---

## Dependencies / Assumptions

- Assumes the existing job-centric fan-out entry point and its pre-filter (`apps/matching/prefilter.py`) remain the layer this retrieval step sits in front of.
- Assumes `classification_tags` (rule-based classification engine) stays the mechanism representing a job's skill/keyword surface; this design concerns tag-based retrieval only and does not touch the embedding-based path.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Technical] How the inverted index stays in sync with `Profile.target_tags` changes — write-through on save vs. a periodic rebuild, and whether it needs the same debouncing `rematch_profile` already uses for rapid successive profile edits.
- [Affects R1][Technical] Storage shape for the inverted index (e.g. a join table vs. a Postgres array/GIN structure) and how it's indexed for the union-lookup query in R2.
- [Affects R2][Needs research] Whether the per-tag union-lookup query needs an explicit candidate cap (top-N) or can rely on the existing pre-filter to bound the result size in practice.
