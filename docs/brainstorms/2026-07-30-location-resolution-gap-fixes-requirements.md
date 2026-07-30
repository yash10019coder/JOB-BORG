---
date: 2026-07-30
topic: location-resolution-gap-fixes
---

# Close the Remaining Location-Resolution Gap: Three Confirmed Data/Logic Bugs

## Summary

The GeoNames-backed v2 dataset (shipped in PR #27, `docs/brainstorms/2026-07-23-geonames-location-coverage-requirements.md`) already lifted `location_resolved=True` from 37% to ~83%. The remaining ~17% unresolved (14,713 of 87,346 `Job` rows as of this brainstorm) isn't random long-tail noise — sampling the top unresolved strings and live-testing them against `normalize_location()` surfaced three concrete, independently-fixable root causes: a country-code-prefix bug that silently drops 75 countries' ISO codes, missing Canadian province abbreviations, and thin common-name synonym coverage for non-hand-curated countries. This brainstorm scopes fixes for those three confirmed causes only — not a broader coverage push.

---

## Problem Frame

Querying live production data shows `location_resolved=True` at 72,633/87,346 (83.2%), i.e. 16.8% unresolved — close to the 14% figure that prompted this brainstorm (likely measured slightly earlier as the dataset grows). Rather than treat "14-17% unresolved" as an undifferentiated tail to slowly whittle down, sampling the 3,690 distinct unresolved location strings by frequency and live-executing `normalize_location()` against them isolated three concrete, high-confidence bugs:

**1. R8's country-code prefix drops 75 countries' ISO codes on incidental collision.** `normalize_location()`'s R8 rule (`"SG - Singapore"`, `"UK - London"`) validates the prefix against `_GeoIndex.country_by_alias` — the same dict used for bare-word lookups. That dict's generation logic (`apps/locations/geodata_generation.py`) drops a country's ISO code entirely whenever it collides with *any* region's admin1 abbreviation anywhere in the world, with no distinction between an intentional collision (worth failing closed on) and an incidental one. Live-checking confirms `"SG - Singapore"` (184 unresolved jobs) fails because Singapore's `SG` collides with Switzerland's St. Gallen canton — an obscure, low-value collision to fail closed on. Enumerating the dataset shows **75 countries** have their own ISO code dropped this way (`AG, AI, AL, AN, AR, AS, AT, AZ, BA, BB, BE, BL, BO, BS, CL, CO, DJ, EC, ER, ES, FR, GA, GE, GF, GL, GP, GR, ID, IL, IS, KY, LA, LI, LU, MA, MD, ME, MF, MG, ML, MN, MO, MQ, MS, MT, NC, NE, NI, NO, PA, PE, PS, PT, RE, SA, SB, SC, SD, SE, SG, SH, SI, SJ, SO, SR, ST, SZ, TF, TG, TM, TN, UM, VA, VG, VI`). About 21 of these also collide with a **US state postal abbreviation** (`GA`=Gabon/Georgia, `AZ`=Azerbaijan/Arizona, `CO`=Colombia/Colorado, `IL`=Israel/Illinois, `MA`=Morocco/Massachusetts, `MD`=Moldova/Maryland, `PA`=Panama/Pennsylvania, `VA`=Vatican/Virginia, etc.) — that subset is an intentional, already-correct exclusion (`"GA - Atlanta"` almost certainly means the US state, not Gabon), not a bug. The other ~54 (including Singapore) are incidental collisions with obscure regions that provide no real signal either way, and currently fail closed for no good reason.

**2. Canadian province abbreviations don't exist in the dataset at all.** `"Toronto, ON"`, `"Calgary, AB"`, `"Vancouver, BC"`, `"Montreal, QC"` all fail — 557 unresolved rows match a `", <2-letter Canadian province code>"` pattern in a live count. Root cause: `apps/locations/geodata_generation.py`'s region-abbreviation logic only fires when GeoNames' admin1 code is alphabetic (`region_code.isalpha()`); Canada's GeoNames admin1 codes are numeric (`CA.08`, not `CA.ON`), so the generator silently produces zero abbreviation aliases for any Canadian province. This is unrelated to the France/Germany-style hand-curation pattern already used for the 5 `COUNTRY_NAME_OVERRIDES` countries — it's a source-data format gap, not a missing curation entry.

**3. Non-hand-curated countries only get GeoNames' literal `name` field as an alias, with no common-English-synonym curation.** Only 5 countries (`US, GB, DE, IN, CA`) have a hand-maintained synonym list in `COUNTRY_NAME_OVERRIDES`; every other country gets whatever GeoNames' raw `name` column happens to contain. Confirmed failures: `"Seoul, Korea"` (only `"south korea"` is aliased, not `"korea"` — 134 unresolved rows contain "korea"), `"Amsterdam, Netherlands"` (only `"the netherlands"` is aliased, not `"netherlands"` — 268 rows), `"Taipei, Taiwan"` (no `"taiwan"` alias at all — 372 rows), plus similar gaps for Hong Kong (77 rows) and Czech Republic (64 rows). These eight terms alone account for ~920 unresolved rows; the true count across all under-aliased countries is certainly higher.

These three causes account for well over 1,500 of the 14,713 unresolved rows from directly-counted evidence alone, likely more once every affected country/pattern is counted rather than just the sampled ones. All three are logic/data bugs in the existing v2 dataset generation and R8 pre-processing, not new coverage work — the underlying GeoNames source data already has what's needed.

Related: `apps/locations/engine.py` (R8 prefix logic, `_resolve_bare`, `_resolve_segments`), `apps/locations/geodata_generation.py` (`_build_countries`, `_build_regions`, `country_abbrev_collisions`), `apps/locations/geodata/v2.yaml`, origin: `docs/brainstorms/2026-07-23-geonames-location-coverage-requirements.md`.

---

## Requirements

**R8 prefix-collision fix**
- R1. The R8 country-code-prefix match uses a dedicated, always-available ISO-alpha2→country lookup, independent of `country_by_alias` (the bare-word-lookup dict that the ambiguity classifier filters). ISO codes are globally unique by construction, so this lookup is never subject to the region-abbrev-collision exclusion that `country_by_alias` applies.
- R2. The ~21 ISO codes that also collide with a US state postal abbreviation (the `GA`/Gabon-vs-Georgia class) are excluded from this dedicated lookup, preserving today's fail-closed behavior for that specific, intentional ambiguity. The exact list is determined during planning/implementation by cross-referencing the 75 collision-dropped codes against the US Census Bureau's 50-state postal abbreviation list (not hand-guessed).
- R3. The remaining ~54 codes (including `SG`) resolve via the dedicated lookup, fixing `"SG - Singapore"` and equivalent country-code-prefixed strings for those countries without affecting the intentionally-excluded 21.

**Canadian province abbreviations**
- R4. Canada's postal province/territory abbreviations (`ON, BC, QC, AB, MB, SK, NS, NB, PE, NL, NT, YT, NU`) are added as region abbreviation aliases, resolving `"City, <province code>"` strings the same way `"City, <US state code>"` already resolves. Source the abbreviation-to-GeoNames-admin1-code mapping explicitly (Canada's own official postal codes), since GeoNames' own admin1 file doesn't provide it directly for this country.

**Country synonym expansion**
- R5. Expand curated common-name synonym coverage beyond the current 5-country `COUNTRY_NAME_OVERRIDES` list to include, at minimum, the countries confirmed present in real unresolved-data samples: South Korea (`"korea"`), Netherlands (`"netherlands"`), Taiwan (`"taiwan"`), Hong Kong, and Czech Republic. Additional countries found during implementation spot-checks against the full (not just top-40) unresolved-string sample may be added at the implementer's judgment, following the existing `COUNTRY_NAME_OVERRIDES` pattern.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3.** Given `location="SG - Singapore"`, when normalization runs, it resolves to `country="SG"`. Given `location="GA - Atlanta"`, when normalization runs, it stays unresolved (unchanged from today) rather than resolving to Gabon.
- AE2. **Covers R4.** Given `location="Toronto, ON"` or `location="Calgary, AB"`, when normalization runs, both resolve to their correct city/province/`country="Canada"`.
- AE3. **Covers R5.** Given `location="Seoul, Korea"`, `location="Amsterdam, Netherlands"`, or `location="Taipei, Taiwan"`, when normalization runs, all three resolve correctly using the informal country name, not just GeoNames' literal name field.

---

## Success Criteria

- `"SG - Singapore"`, Canadian `"City, <province>"` strings, and the five confirmed country-synonym patterns (Korea, Netherlands, Taiwan, Hong Kong, Czech Republic) all resolve correctly after this ships, without a version-cutover dry-run regression (per the existing `backfill_locations --dry-run` safety net from PR #27).
- `"GA - Atlanta"` and other US-state-colliding country-code prefixes continue to stay unresolved — no new "confidently wrong" resolutions introduced.
- The overall `location_resolved=True` rate rises measurably above 83.2% after the sweep re-normalizes existing rows, though no specific target percentage is set — this round targets the three confirmed root causes, not a coverage percentage goal.

---

## Scope Boundaries

- Reversed `"Country - City"` order (e.g. `"Brazil - Rio de Janeiro"`), non-comma delimiters (`>`, `•`), suffix noise beyond the existing `" Area"` handling (`"Office"`, `"Metropolitain Area"`, parenthetical addresses like `"(Corporate)"`), `"N Locations"` placeholders, and doubled/malformed prefixes (`"UK - UK - London"`) are all real patterns visible in the unresolved sample, but out of scope for this round — each needs its own design pass and wasn't part of the "fix confirmed bugs" decision this brainstorm scoped to.
- A systemic country-synonym curation overhaul (hand-curating common names for all ~190 countries, not just the 5 confirmed by real data) is out of scope. R5 covers only the countries confirmed present in the sampled data; broader curation is deferred until it recurs as evidence in future unresolved-data sampling.
- Below-minimum-population cities (< 15,000, the existing `DEFAULT_MIN_POPULATION` threshold) remain unresolved — unchanged from the v2 dataset's existing scope boundary.
- No change to `apps/matching/scoring.py` or the admin `location_resolved` filter's semantics — this work only improves what `normalize_location()` can resolve, consistent with the origin brainstorm's same boundary.

---

## Key Decisions

- Dedicated ISO2 lookup over a narrow Singapore-only exemption: the collision-drop rule affects 75 countries, not one. A single-country patch fixes today's visible symptom but leaves the other ~53 broken until each is independently hit in production and re-diagnosed from scratch. The dedicated-lookup fix is the same size of change with a much larger payoff, and is architecturally cleaner — it separates "is this ISO code globally unique" (always true) from "is this bare token ambiguous as a general word" (the bare-alias dict's actual job).
- Preserve the US-state-collision exclusion explicitly (R2) rather than let the dedicated lookup cover all 75 uniformly: confirmed via direct discussion that resolving `"GA - Atlanta"` to Gabon would be a new instance of the "confidently wrong is worse than unresolved" failure class this dataset's design already guards against elsewhere (see `docs/solutions/logic-errors/onsite-only-location-filter-ignores-target-locations.md`).

---

## Dependencies / Assumptions

- Assumes the exact list of ~21 US-state-colliding ISO codes can be derived mechanically (cross-referencing GeoNames' collision data against the US Census Bureau's standard 50-state postal abbreviation list) rather than requiring manual enumeration — not separately verified in this brainstorm, flagged for planning/implementation.
- Assumes Canada's official postal province/territory abbreviations (13 total) are stable, well-known public data not requiring GeoNames sourcing.
- Assumes the existing `backfill_locations --dry-run` / `--strict` / `--json` safety net (added in PR #27's code-review round) is sufficient to catch any unintended regression before a version cutover for this change — no new verification tooling is assumed necessary.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1, R2, R3][Technical] Whether the dedicated ISO2 lookup lives in `apps/locations/geodata_generation.py` (as a new field in the generated dataset) or as a small hand-maintained constant in `apps/locations/engine.py` directly (mirroring `_TWO_LETTER_PREFIX_RE`'s existing sibling constants) — a build-time-generated vs. hand-maintained-constant tradeoff to resolve during planning.
- [Affects R2][Needs research] Exact enumeration of the ~21 US-state-colliding codes, cross-referenced against the current 75-entry collision list in the live-generated `v2.yaml` (this list will drift slightly as GeoNames data updates, so it should be derived programmatically at generation time, not hardcoded from this brainstorm's one-time enumeration).
- [Affects R5][Needs research] Whether to do a broader (but still evidence-gated) scan of the full 3,690-distinct-unresolved-string list for additional under-aliased countries beyond the five confirmed here, during implementation, rather than treating the five as a hard ceiling.
