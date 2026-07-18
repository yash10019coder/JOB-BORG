---
title: "feat: Search bar for recommendations (title/description, Postgres full-text)"
type: feat
status: completed
created: 2026-07-18
---

# feat: Search Bar for Recommendations

## Context

The recommendations page (`apps/web/views.py::recommendations`) already lets a
user toggle between recommended-only and all-scored-matches (`?all=1`), but
there's no way to narrow that list by keyword. As match counts grow, finding
a specific role or company by name/skill mention becomes a scroll-and-scan
exercise.

This adds a search box that full-text searches the logged-in user's own
`UserJobMatch` rows by the related job's `title` and `description`, using
Postgres full-text search (`django.contrib.postgres.search`). Search is
**layered on top of** the existing `show_all` toggle — confirmed with the
user: search further narrows whichever toggle state (recommended-only vs
all-matches) is currently active, it does not replace or bypass it.

## Scope Boundaries

**In scope:** a `q` GET-param search box on the recommendations page; a
Postgres full-text index on `Job.title`/`Job.description`; search combined
(AND) with the existing `show_all` filter and the existing dismissed-job
exclusion; search state preserved across pagination and toggle links.

**Out of scope (non-goals):**
- Searching jobs the user has no `UserJobMatch` row for (global job search)
  — explicitly resolved with the user: search stays scoped to the user's own
  matches, not a separate global browse feature.
- Search-driven re-ranking of `match_score` (search only filters, it does not
  change scoring/ordering — matches remain ordered `-match_score` unless a
  ranked-by-relevance mode is added later).
- Fuzzy/typo-tolerant search, synonyms, or autocomplete.
- Filtering by other facets (location, salary, remote) — a follow-up if this
  proves useful, not part of this plan.
- Non-English job postings: Postgres FTS with `config="english"` stems
  English text; non-English postings won't error but will silently
  under-match (fewer stemmed hits, not a substring fallback). Acceptable for
  now since all currently-ingested boards are English-language; revisit if
  that changes.

### Deferred to Follow-Up Work
- Search-relevance ranking (`SearchRank`) as an alternate sort mode, if
  keyword relevance turns out to matter more than match score for some
  searches.
- A global (all-jobs, not just matched) search view, if users want to browse
  the full catalog independent of their profile's scoring.

## Implementation Units

### U1. Postgres full-text index on Job title/description

**Goal:** `Job` rows are indexed for full-text search on `title` +
`description` without adding a new stored column, matching the repo's
existing preference for plain field indexes over triggers/generated columns.

**Dependencies:** none

**Files:**
- `apps/jobs/models.py` (modify — add the index to `Job.Meta.indexes`)
- `apps/jobs/migrations/0003_job_search_gin.py` (create)

**Approach:** Add an expression-based `GinIndex` on
`SearchVector("title", "description", config="english")` directly in
`Job.Meta.indexes` (`apps/jobs/models.py`), then generate the corresponding
migration. This is the closest fit to the existing index style
(`job_tags_gin`, `job_status_idx`, etc. — all plain `Meta.indexes` entries,
no denormalized columns or signals) and avoids introducing a
`SearchVectorField` + population-trigger pattern that doesn't exist
elsewhere in the codebase. Name the index `job_search_gin` to match the
existing `<model>_<purpose>_gin` convention.

**Critical implementation detail — explicit `config` is required, not
optional:** `SearchVector("title", "description")` with no `config=` kwarg
compiles to a single-argument `to_tsvector(...)` call that resolves its text
-search configuration via `get_current_ts_config()` at query/index-build
time — and Postgres rejects that as a non-`IMMUTABLE` function in an index
expression (`CREATE INDEX ... USING gin (...)` fails with "functions in
index expression must be marked IMMUTABLE"). This was verified directly
against the project's own `pgvector/pgvector:pg16` container — the migration
in this unit **will not apply** without an explicit, literal `config="english"`
on the `SearchVector(...)` call. U2's `SearchVector`/`SearchQuery` calls must
use the same literal `config="english"` for the query to actually use this
index rather than mismatch on config and force a sequential scan.

**Patterns to follow:** `apps/jobs/models.py` `Meta.indexes` block (existing
`GinIndex(fields=["classification_tags"], name="job_tags_gin")`);
`apps/jobs/migrations/0002_job_source_url.py` for migration file shape.

**Test scenarios:**
- Migration applies cleanly on top of `0002_job_source_url` and is reversible
  (`migrate jobs 0002` then forward again) without error.
- `Test expectation: no dedicated unit test for the index itself` — its
  effect is exercised end-to-end by U2's search-filtering tests, which would
  fail to find matches if the search vector/index were misconfigured.

**Verification:** `python manage.py migrate` applies without error;
`python manage.py sqlmigrate jobs 0003_job_search_gin` shows a `CREATE INDEX
... USING gin (...)` statement referencing `title`/`description`. After U2
lands, an informal `EXPLAIN` on the search query should show a Bitmap Index
Scan on `job_search_gin`, not a sequential scan — confirms the query's
`config="english"` actually matches the index rather than silently missing
it.

---

### U2. Search filtering in the recommendations view

**Goal:** `GET /?q=<term>` narrows the current user's matches (respecting
whatever `show_all` state is active) to jobs whose title or description
matches the search term; combined with `all=1` narrows the all-matches view;
omitting `q` behaves exactly as today.

**Dependencies:** U1

**Files:**
- `apps/web/views.py` (modify — `recommendations` view)
- `apps/web/tests/test_recommendations.py` (modify — extend `_job` factory,
  add search test scenarios)

**Approach:** Read `query = request.GET.get("q", "").strip()` alongside the
existing `show_all` read. When `query` is non-empty, apply
`.annotate(search=SearchVector("job__title", "job__description", config="english")).filter(search=SearchQuery(query, config="english"))`
as an additional filter in the existing chain — after the `show_all`
conditional filter, before `Paginator(...)`, mirroring how `show_all` is
already applied as a conditional `.filter(...)` in the current chain. The
`config="english"` must match U1's index exactly (see U1's implementation
note) — a mismatched or default config still returns correct results but
silently bypasses the GIN index (sequential scan instead of a bitmap index
scan). `SearchQuery`'s default `search_type="plain"` ANDs all terms in a
multi-word query (a query must match all words, not any) — worth surfacing
in the empty-state copy (see U3) since a two-word query with no single
posting containing both terms will correctly return zero results, which can
otherwise read as broken rather than as expected AND-semantics. Pass
`query` into the template context alongside `page_obj`/`show_all` so it can
be redisplayed in the search input and appended to toggle/pagination links.
Dismissed-job exclusion and `-match_score` ordering are unaffected — search
is purely an additional AND-ed filter, consistent with how `show_all`
already composes with the dismissed-exclusion in the current implementation.

**Patterns to follow:** the existing conditional-filter shape in
`recommendations()` (`if not show_all: matches = matches.filter(...)`) —
mirror this exact style for the `q` conditional rather than introducing a
different filtering idiom.

**Test scenarios:**
- Happy path: a match whose job title contains the search term is returned;
  a match whose job title/description does not contain the term is excluded.
- Description-only match: search term appears only in `description`, not
  `title` — still returned (proves both fields are indexed/searched).
- Empty query (`q=` or omitted): behaves identically to today — no
  regression on the two existing toggle tests.
- Combined with toggle: `q=<term>&all=1` returns below-threshold matches
  that match the term; `q=<term>` (no `all`) excludes below-threshold
  matches even if they match the term (search narrows, doesn't override the
  toggle's status filter) — Covers the user's explicit requirement that
  search layers on top of the toggle.
- Dismissed exclusion still applies: a dismissed job matching the search term
  is still excluded, mirroring `test_show_all_still_excludes_dismissed`.
- No results: a query matching no job returns an empty `page_obj` without
  error (feeds U3's empty-state copy).
- Case-insensitivity / multi-word: Postgres FTS's default English config
  handles case-insensitive and stemmed matches (e.g. `"engineer"` matches
  `"Engineering"` per FTS stemming) — one scenario asserting this isn't
  purely a literal substring match, to make the FTS choice visible in tests
  rather than accidentally passing the same way `icontains` would.
- Cross-user isolation: user B's matches are never returned for user A's
  search, mirroring the existing `test_ranked_recommended_only_for_logged_in_user`
  isolation check.

**Verification:** the new and existing tests in
`apps/web/tests/test_recommendations.py` pass; manually confirmed via
`curl "http://localhost:8000/?q=<term>"` (authenticated session) returning
the filtered set.

---

### U3. Search bar UI + state-preserving links

**Goal:** the recommendations page shows a search input (pre-filled with the
current query), submitting it re-filters the page; the toggle link and
pagination links preserve the active search term so paging or toggling
doesn't silently drop it; a distinct empty-state message appears when a
search returns zero results (vs. the existing "no matches at all" copy).

**Dependencies:** U2

**Files:**
- `templates/web/recommendations.html` (modify)
- `apps/web/tests/test_recommendations.py` (modify — template-rendering
  assertions for the search input and empty-state copy)

**Approach:** Add a `<form method="get">` search input near the existing
header (`<h1>`/toggle-button div), submitting `q` as a GET param (so it's
bookmarkable/shareable and composes naturally with `?all=1` via the browser's
normal form-GET query-string merge — the form should also carry a hidden
`all` field mirroring the current `show_all` state, so submitting search
doesn't reset the toggle). The input needs a visible `<label>` (or
`aria-label`), placeholder copy (e.g. "Search by title or keyword"), and an
explicit visible submit button — do not rely on implicit Enter-to-submit as
the only affordance. Update the toggle link (`?all=1` / recommendations
root) and the two pagination links to append `&q={{ query|urlencode }}` when
`query` is truthy, matching the existing `{% if show_all %}&all=1{% endif %}`
conditional-append style already used for pagination.

When `query` is truthy and `page_obj` is non-empty, show a small results-
summary line above the list (e.g. `Showing results for "{{ query }}"`) with
a plain link back to the current toggle state without `q` — this doubles as
the "clear search" affordance while results exist, not just in the empty
case. Add a third empty-state branch: when `query` is set and `page_obj` is
empty, show "No matches found for '{{ query }}'." with the same clear-search
link, distinct from the existing two branches (`show_all` empty vs
recommended-only empty) which stay unchanged. Do not alter the existing
"No recommendations above the threshold yet" string used when `query` is
absent — `test_empty_state_renders_friendly` asserts on it directly.

**Patterns to follow:** the existing toggle-link conditional-append pattern
in `templates/web/recommendations.html` pagination block; the existing
two-branch empty-state `{% if show_all %}...{% else %}...{% endif %}`
structure, extended rather than restructured.

**Test scenarios:**
- Happy path: rendering with `?q=python` shows the search input pre-filled
  with `python` (`value="python"` in the rendered HTML).
- Toggle link preserves search: rendering with `?q=python` (no `all`) shows
  the "Show all matches" link containing both `all=1` and `q=python`.
- Pagination preserves search: with enough matches to paginate and
  `?q=<term>`, the "Next" link contains `q=<term>`.
- Search-empty-state: a query with zero results renders "No matches found
  for" (not the generic "No recommendations above the threshold yet"
  string) and does not error.
- No regression: `test_empty_state_renders_friendly` (asserts the exact
  existing string with no `q` param) continues to pass unchanged.

**Verification:** full `apps/web/tests/test_recommendations.py` suite green;
manually exercised in a browser — search a term, confirm results narrow,
toggle "Show all matches" while a search is active and confirm the search
term stays applied, page through multi-page search results and confirm the
term stays applied on both Prev/Next.

---

## Key Technical Decisions

- **Scope: user's own matches, not a global job search** — resolved directly
  with the user (search "should honor the toggle show recommended jobs only
  and add search on top of it"). Keeps the feature personalized and reuses
  the existing `UserJobMatch` query rather than introducing a parallel
  all-jobs browse view.
- **Postgres full-text search over simple `icontains`** — resolved with the
  user. Chosen over substring matching for stemming and multi-word query
  handling (e.g. `"engineer"` matching `"Engineering"`), not for relevance
  ranking — this plan does not re-sort by relevance (see the AND-ed-filter
  decision below), so the justification is scoped to what this feature
  actually delivers. Costs one new migration (GIN index) that the repo
  doesn't currently have infrastructure for.
- **Expression-based `GinIndex` on `SearchVector(...)`, no new model field**
  — closest fit to the repo's existing plain-`Meta.indexes` style (no
  triggers, no denormalized `SearchVectorField` + signal-based population
  pattern). Trade-off: the search vector is computed at query time rather
  than stored. Concrete revisit trigger (not just "if it gets big"): if the
  `jobs` table exceeds roughly 100k rows, or an `EXPLAIN ANALYZE` on the U2
  query shows the planner falling back to a sequential scan instead of a
  bitmap index scan on `job_search_gin`, switch to a stored
  `SearchVectorField` populated via a `pre_save`/bulk-update hook.
- **Search composes as an AND-ed filter, not a ranking mode** — search
  narrows the existing `-match_score`-ordered list; it does not re-sort by
  keyword relevance. Keeps this plan's surface small; relevance ranking is
  deferred (see Scope Boundaries) since it's a genuinely separate UX decision
  (do users want "best match" or "best keyword hit" first?) not implied by
  the user's request.

## Verification (End-to-End)

1. `python manage.py migrate` applies the new GIN index migration cleanly.
2. `apps/web/tests/test_recommendations.py` full suite green (existing +
   new search scenarios), plus the full project suite has no regressions.
3. Log in, land on `/` with existing matches, type a term matching a known
   job's title into the search box, submit — list narrows correctly.
4. Toggle "Show all matches" while a search term is active — search stays
   applied and now surfaces below-threshold matches matching the term too.
5. Page through a multi-page search result set — search term persists across
   Prev/Next.
6. Search a term with zero matches — friendly "No matches found" empty
   state, no error.
7. Clear the search — full toggle-scoped list reappears exactly as before
   this feature existed.
