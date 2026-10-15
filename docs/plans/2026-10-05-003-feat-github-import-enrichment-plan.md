---
title: "feat: GitHub import enrichment - projects and skills with evidence (follow-up to Phase 4, epic #117)"
type: feat
status: completed
origin: docs/brainstorms/2026-10-05-github-import-enrichment-requirements.md
depth: standard
---

# GitHub import enrichment: projects + skills with evidence

## Summary

Extend the Phase 4 GitHub import (`apps/accounts/importing/github.py`, PR #137) so it proposes a curated list of the user's **own projects** and a **profile-level skill list with proof**, instead of only a profile link, a portfolio link and one or two job-matching tags. Public data only; every item is reviewed; nothing the user set is overwritten; GitHub time is never added to job years. Revised after a document review (coherence, feasibility, security, adversarial): the findings and how each was resolved are recorded at the end.

## Problem Frame

The current import reads one page of up to 30 repos and keeps only the primary language of each, mapped through the 14-tag job-matching vocabulary, so a typical user gets one or two tags and no projects. See origin: `docs/brainstorms/2026-10-05-github-import-enrichment-requirements.md` (R1-R11, AE1-AE4). The profile also has no profile-level skill list: skills exist only on individual `ResumeEntry` rows.

## Decisions (settled with the user)

- Scope: projects + skills with evidence. Contributions (PRs/issues) and bio-derived fields are deferred.
- **Skills seen in 2+ repos start ticked**; 1-repo skills and all projects start unticked. This deliberately overrides the origin's "everything unticked" for skills only; the origin doc is updated to match.
- **Token:** `GITHUB_API_TOKEN` is recommended (a no-scope token, public read only). Without it the import runs in light mode and says so; framework skills (AE2) need the token.
- **Throttle:** a per-user cooldown plus a server-wide ceiling that downgrades to light mode when quota is low (GHX1).
- **Deleting a skill is remembered:** the row is kept hidden and the next import shows it unticked with a "you removed this" note.
- **Ownership:** the import only runs for the login in the profile's saved GitHub link. A user with no saved link is asked to add it on the Profile page first. This is stricter than the label-only option; it also affects the existing basic GitHub import (see Risks).
- GitHub years are shown separately ("seen on GitHub since YYYY") and never added to job years.
- Public data only: no OAuth, no pasted token.
- Stacked on `feat/profile-import` (PR #137): branch `feat/github-import-enrichment`.

## Design

### Data fetched (`apps/accounts/importing/github.py`)

All calls keep the existing safety: no redirects, timeouts, coded errors, whitelisted fields, nothing from a response logged.

- **URL construction.** Every URL is built from a fixed template on `https://api.github.com` with the validated login and a repo name matching `^[A-Za-z0-9._-]{1,100}$` (not `.` or `..`), each `quote()`d. URLs inside responses (`download_url`, `contents_url`, `html_url`, `url`) are never requested. The `Authorization` header is attached only to `api.github.com`.
- **Light mode (always; 2-5 calls):** `/users/<u>` and `/users/<u>/repos?type=owner&sort=pushed&per_page=30`, up to 3 pages (90 repos). Whitelist adds `description`, `topics`, `created_at`, `pushed_at`, `size`, `is_template`. Skill evidence comes from own, non-fork, non-archived, non-template repos with `size` above a small minimum; each contributes its primary `language` and `topics`.
- **Size caps:** 256 KB for single-object calls, 600 KB for repo-list calls, 64 KB for manifest bodies. Exceeding a cap on the first two calls is `github_error`; on an enrichment call it is "no data" (below).
- **Full mode (token set), shortlist only (top 8 repos):**
  - `/repos/<u>/<r>/languages`: keep languages with at least 10% of the repo's bytes.
  - `/repos/<u>/<r>/contents/`: root listing, then fetch (raw `Accept`, 64 KB cap) only known manifests present: `package.json`, `requirements.txt`, `pyproject.toml`, `go.mod`, `Gemfile`, `composer.json`, `pom.xml`, `build.gradle(.kts)`, `Dockerfile`.
  - Pinned repos through one GraphQL call. Pinned repos are filtered by the same own / non-fork / non-archived rules and must also appear in the fetched repo set; a pinned repo outside it is ignored.
- **Failure semantics.** After the first two calls succeed, any error on a per-repo call (404, 3xx, non-200, oversize, parse failure, GraphQL error) means "no data from this repo or source": the import continues. Only a rate limit, the 80-call cap or the 45-second budget stops enrichment, setting `meta.mode = "partial"`. A failure of the first two calls fails the import as today.
- **Budget:** at most **80 calls** and **45 seconds** per import (under the 90 s task soft limit); the budget is checked between calls. Worst case per shortlisted repo is about 7 calls, hence 8 repos.
- **Throttle (GHX1).** In addition to the existing 10-per-hour per-user limit, a **5-minute cooldown between GitHub imports per user** and a **server-wide hourly counter**. The client records `X-RateLimit-Remaining` from the latest response; when it is below 500 (token) or below 15 (no token), new imports run in light mode, and with no quota left they fail with `github_rate_limited`. Without a token the 60-per-hour ceiling is shared by every user, so light mode costing 2-5 calls allows roughly 12-30 imports an hour server-wide; this is stated in the deploy notes.
- `GitHubClient` gains `fetch_profile(username, full)` returning a plain dict; the existing `fetch()` stays until its callers are migrated. The client sends GET with a per-call `Accept` header and a per-call byte cap, and one POST for GraphQL.

### Ownership (GHX0)

The importing user's profile must have a saved `github_url`; the requested login must equal the login in it (case-insensitive). The form is pre-filled with that login. With no saved link the page says "Add your GitHub link on your Profile page first", and nothing is fetched. This also applies to the existing basic import, whose `github_url` proposal becomes redundant and is dropped. Stored skills and projects are labelled "from your public GitHub activity".

### Skills (new `apps/accounts/importing/github_skills.py`, pure, no I/O)

- Evidence per repo: primary language, languages at least 10% (shortlist), topics, manifest packages (shortlist).
- Names are canonicalised through `skills_lexicon.canonical_skill()`. Linguist language names outside the lexicon are kept only if in a small `LANGUAGE_ALLOW` set; noise (Makefile, Batchfile, Procfile, incidental Shell) is dropped; "Jupyter Notebook" maps to Python. Topics count with the same weight as languages but only when they map through the lexicon.
- Manifest dependencies map **only** through a new versioned `PACKAGE_SKILLS` table in `skills_lexicon.py` (for example react, next, express, django, flask, fastapi, gin, rails, spring-boot, tokio, laravel); `SKILLS_LEXICON_VERSION` is bumped. An unknown package proposes nothing. A `Dockerfile` proposes Docker.
- **Manifest parsing is hostile-input-safe.** Text only, never executed. Each parser runs inside a catch-all (including `RecursionError`), rejects any file with a line over 2,000 characters or nesting deeper than 20, and uses linear-time patterns (no nested quantifiers): `json.loads` (package.json, composer.json), `tomllib` (pyproject), line scans (requirements, go.mod, Gemfile), `artifactId` scan (pom.xml), dependency-line scan (gradle). A failure yields nothing for that file.
- **Evidence schema (fixed, validated again on apply):** `repo_count` (int), `scope` (`"recent"` for language/topic evidence over the scanned recent repos, `"top"` for manifest/percentage evidence over the shortlist), `scope_size` (int), `first_seen` and `last_seen` (`YYYY-MM`), `repos` (at most 5 names matching the repo-name pattern), `sources` (subset of `language`, `topic`, `package`). Anything else is dropped.
- **Proof line** states its scope and shows the dates separately: "Python · 14 of 90 recent repos · first 2019 · last 2026"; "Django · 3 of your top 8 repos · first 2021 · last 2026". Default tick when `repo_count >= 2`. At most 40 skills, ranked by repo count then recency.
- Skills that are job-matching tags still produce today's `target_tags` field proposal.
- A skill the user previously removed (stored as dismissed) is proposed unticked with a "you removed this" note.

### Projects (same module)

- Candidates: own, non-fork, non-archived, non-template repos. Ranking: pinned first (full mode), then stars (log-weighted), recently pushed (12 months), has a description, non-trivial size; ties by name. **10 shown, up to 25 in the payload** (the rest behind a no-JS `<details>`).
- Entry proposal in the existing `entries` payload shape: `kind=project`, title = repo name, `description` (at most 300 chars, through the same `clean_text` rules plus removal of bidi and zero-width characters), `url` = **rebuilt as `https://github.com/<login>/<repo>`** (never copied from the response), start = `created_at` month, end = `pushed_at` month, `skills` = that repo's own evidence only (at most 30), snippet "★ 12 · updated Mar 2026", `default_accept=False`, `extractor="github"`.
- **Pre-validated.** A project that `clean_entry` would reject is normalised or dropped at propose time: title shorter than 2 or longer than 80 characters, trailing `.`, or end before start (end is then set equal to start). `_entry_data` in `review.py` is extended to carry `description` and `url` from the proposal into `apply_entries`.
- **Identity.** Matching an existing project uses the repo URL first, then falls back to title + start month, so an edited title or a renamed repo resolves to "kept" instead of a duplicate.
- Projects are `kind=project`, so `years_by_skill` (experience only) already ignores them.

### Storage

- `ResumeEntry` gains `description` (text) and `url` (https URL, 255), validated in `resume_facts.clean_entry` (host must be `github.com` for GitHub-sourced entries) and compared in `_COMPARED`.
- New `ProfileSkill` model: `profile` FK (`related_name="profile_skills"`), `name`, `key` (casefolded), `origin` (`github`), `evidence` JSON, `dismissed` boolean (default false), timestamps; `UniqueConstraint(profile, key)`. **No `source` column** (nothing creates or edits skills by hand yet). The already-edited `apps/accounts/models.py` is corrected to match before migration `0015` is generated.
- New `apps/accounts/services/profile_skills.py`: `apply_skills(profile_id, skills, mode)` upserts by key, validates evidence against the fixed schema, and **never deletes**. A light or partial run merges instead of replacing (larger `repo_count`, earliest `first_seen`, latest `last_seen`); a full run replaces. `dismiss_skill(profile, pk)` (owner-scoped) sets `dismissed=True`; dismissed rows are hidden. It runs inside the existing `apply_review` transaction so fields, entries and skills are all-or-nothing.
- Nothing reads `ProfileSkill` to answer application questions in this change (origin R8): it is stored and displayed only. A later feature that answers from it must treat it as unverified.

### Review and UI

- `run_github_job` payload: `{"fields", "entries": [projects], "skills": [...], "meta": {"source": "github", "mode": "full|light|partial", "scanned": N}}`; `PAYLOAD_VERSION` bumped; `build_review` tolerates payloads without `skills`.
- `review.py`: `SkillRow` (name, proof, existing new/unchanged/dismissed, accept) and `skill_decision__<i>` parsing; skill names are not editable. Entry rows gain `description` and `url`.
- Review page: a **Skills** section with scoped proof lines; a banner for light mode ("Without a GitHub token only each repo's main language and topics were read") and partial mode ("GitHub limited requests; some repositories were not read"); a note that evidence reflects public activity only; projects 10 + `<details>`.
- Import tab: a **Skills** card with a remove button (`import_skill_dismiss`, owner-scoped POST mirroring `import_entry_delete`); project cards show description and URL as text; the years-by-skill line is unchanged. The GitHub form is pre-filled from the saved link and shows the "add your GitHub link first" state when it is missing.

### Rules contract

Add a **GHX** group to `docs/plans/2026-10-05-002-profile-import-rules.md`: GHX0 ownership, GHX1 URL construction, throttle and budgets, GHX2 failure semantics and light/partial modes, GHX3 skill sources, noise and template filter, GHX4 manifest parsing limits, GHX5 package table, GHX6 evidence schema, proof line and default tick, GHX7 project ranking, caps, pre-validation and identity, GHX8 storage, merge and dismiss, GHX9 years separation, GHX10 token handling. `RuleCoverageTests` fails until each ID has a citing test.

## Implementation units

Dependencies: G1 first; G2 and G3 are independent of each other and both depend on G1 only for the evidence schema; G4 depends on G1, G2 and G3; G5 on G4; G6 last.

- **G1. Models + write API.** `ResumeEntry.description/url`, `ProfileSkill` (with `dismissed`, no `source`), migration 0015, `profile_skills` service, `clean_entry` changes. Files: `apps/accounts/models.py`, `apps/accounts/admin.py`, `apps/accounts/services/resume_facts.py`, `apps/accounts/services/profile_skills.py`. Tests: upsert, evidence schema rejection, light/partial merge keeps richer evidence, dismiss hides and survives re-import, never-delete, owner-scoped dismiss, github.com-only URL, description cleaning, `years_by_skill` unaffected by projects.
- **G2. Derivation.** `github_skills.py`, `PACKAGE_SKILLS`. Tests per manifest type (valid, malformed, oversized), hostile inputs (deep nesting, one 64 KB line, regex-stress strings), language threshold, noise and template filter, topic mapping, scoped evidence and proof line, default tick, dismissed skills unticked, project ranking, caps, pre-validation (short title, trailing dot, end before start), URL rebuilt not copied.
- **G3. Fetcher.** Light/full modes, URL templates and repo-name validation, size caps, pagination, call and time budgets, per-repo best-effort, partial results, raw-manifest `Accept`, GraphQL only with a token, throttle and quota downgrade, ownership check. Tests with the injected fake session in `apps/accounts/tests/test_import_github.py`: a hostile `download_url` is never requested, the token is only sent to `api.github.com`, redirects never followed, sentinel never logged, a 404 on one repo does not fail the import, budget exhaustion yields partial, cooldown and ceiling.
- **G4. Service + review + apply.** Payload v2, skill rows, defaults, `_entry_data` carrying description and url, atomic apply. Tests: forced failure writes nothing, re-import deletes nothing, URL-first project matching (AE4), old payloads still render.
- **G5. Web.** Skills section, banners, ownership state, Import tab card and dismiss route; ownership 404s and CSRF tests in `apps/web/tests/test_import_views.py`.
- **G6. Docs and wrap-up.** GHX rules, origin doc F1 and Key Decisions updated for the skill default, requirements status, full test run, `makemigrations --check`.

## Risks

- **Basic import behaviour changes.** Requiring a saved GitHub link means the Phase 4 flow "enter a username, get a `github_url` proposal" no longer works for a profile with no saved link. Reversible by relaxing GHX0 to a label-only note.
- Public rate limit (60/hour per server IP) without a token is shared by every user; the throttle and light mode keep it usable but the ceiling is low. Setting `GITHUB_API_TOKEN` is the intended deployment.
- A skill list inflated by incidental languages or spam topics: mitigated by the 10% threshold, noise and template filters, scoped proof lines and unticked 1-repo skills (an owner of two spam repos can still produce one ticked skill; the user reviews every item).
- Stored text comes from a third party: descriptions are cleaned, evidence follows a fixed schema, URLs are rebuilt, all rendered as escaped text.
- Repo creation date is used as "first seen"; it can differ from real usage.

## Verification

1. `python manage.py test --noinput apps.accounts apps.web` and `makemigrations --check`.
2. Live with a throwaway user (saved GitHub link set to a real account), dev stack pointed at this worktree and migrated: without a token (light banner, scoped proof lines, 2+-repo skills ticked, projects unticked); with a token (frameworks from manifests, pinned repos first); apply, then confirm the Import tab shows skills and projects and job years are unchanged; remove a skill and re-import (shown unticked with the note); re-import keeps edits and deletes nothing; a username that does not match the saved link is refused before any request.

## Review record

Document review round 1 (coherence, feasibility, security, adversarial): 17 distinct findings. Resolved in this revision: skill default-tick deviation recorded and the origin doc updated; light/partial overwrite of richer evidence; per-repo failures no longer fail the import; project pre-validation and `description`/`url` carried through apply; hostile manifest input; request-path and token handling; URL rebuilt from validated parts; evidence schema; proof-line scope; template and tiny repos excluded; project identity by URL; `source` column removed; token guidance. Decided by the user: rate limit and token policy, remembered skill removal, ownership by saved link. Not addressed: first-seen from repo creation date (noted in Risks); topic spam is only partly mitigated.
