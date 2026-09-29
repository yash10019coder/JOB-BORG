---
title: "feat: Add GitHub Actions CI/CD for pull requests"
type: feat
status: completed
date: 2026-09-29
---

# feat: Add GitHub Actions CI/CD for pull requests

## Summary

Add GitHub Actions so every PR (and every push to `master`) runs the hermetic Django test suite plus migration and Docker build checks, and so a green `master` publishes a production image to GHCR. Deployment to a runtime target is deliberately left out until one exists.

---

## Problem Frame

The repo has no `.github/` directory: PRs (several open right now, e.g. the `auto_apply` fixes) are merged with no automated test, migration-drift, or image-build signal. `config/settings/test.py` was already written to be "hermetic" for CI, but nothing consumes it.

---

## Assumptions

*This plan was authored without synchronous user confirmation. The items below are agent inferences that fill gaps in the input — un-validated bets that should be reviewed before implementation proceeds.*

- "CI/CD for the PRs" means CI gating on PRs is the priority; CD is limited to publishing an image, since no deploy target is named anywhere in the repo.
- GHCR (via the built-in `GITHUB_TOKEN`) is an acceptable registry; no external secrets are available.
- No linter is in the dependency set, so linting is out of scope rather than introduced unasked.

---

## Requirements

- R1. PRs and pushes to `master` run the full test suite against Postgres+pgvector using `config.settings.test`.
- R2. CI fails on missing migrations and on Django system-check errors.
- R3. CI verifies the Dockerfile still builds.
- R4. A successful CI run on `master` publishes a production-dependency image to GHCR, tagged `latest` and by commit SHA.
- R5. Dependency and action versions are kept fresh automatically.
- R6. The workflow is documented so contributors know what gates a merge.

---

## Scope Boundaries

- No deploy job, environment, or infrastructure provisioning.
- No linter/formatter/type-checker introduction.
- Branch protection rules are a repo-settings action, not code.

### Deferred to Follow-Up Work

- Deploy job appended to the CD workflow: once a hosting target is chosen.
- Ruff (lint + format) CI step: separate PR that also fixes existing violations.
- `backfill_locations --dry-run --strict` as a CI step: AGENTS.md notes it is not a hard gate.

---

## Context & Research

### Relevant Code and Patterns

- `config/settings/test.py`: eager Celery + locmem cache; suite needs only Postgres (pgvector image, matching `docker-compose.yml`).
- `Dockerfile`: `ARG REQUIREMENTS=requirements/dev.txt` allows a base-only production build.
- `requirements/dev.txt`: only adds `responses` over `base.txt`.
- `config/settings/base.py`: `DJANGO_SECRET_KEY` has a default and `DATABASE_URL` is env-driven, so no secrets are needed in CI.
- AGENTS.md documents the exact test command and env vars.

### Institutional Learnings

- `docs/solutions/` only covers location-filter logic; nothing CI-relevant.

### External References

- Standard `actions/setup-python`, `docker/build-push-action`, `docker/metadata-action` usage; GitHub `workflow_run` runs only from the default branch's workflow definition.

---

## Key Technical Decisions

- Service container over docker-compose in CI: faster, no image build needed for tests; uses same `pgvector/pgvector:pg16` image as local dev.
- CD triggered by `workflow_run` on CI success rather than duplicating tests: guarantees only tested SHAs are published; checks out `head_sha` explicitly.
- Production image built from `requirements/base.txt`: keeps test libs out of the artifact.
- Least-privilege `permissions:` per workflow; `packages: write` only in CD.
- `concurrency` cancel-in-progress on CI to save minutes on force-pushes.

---

## Open Questions

### Resolved During Planning

- Where does CD deploy? Nowhere yet; publish-only.

### Deferred to Implementation

- Whether GHCR package visibility needs manual adjustment after the first push.

---

## Implementation Units

### U1. CI workflow

**Goal:** Gate PRs on tests, migration drift, system checks, and Docker build.

**Requirements:** R1, R2, R3

**Dependencies:** None

**Files:**
- Create: `.github/workflows/ci.yml`

**Approach:**
- `test` job with pgvector service + health check, Python 3.12 with pip cache, `check`, `makemigrations --check --dry-run`, `test`.
- Separate `docker-build` job (no push, GHA layer cache).

**Patterns to follow:**
- Test env vars exactly as in AGENTS.md.

**Test scenarios:**
- Integration: open a PR with a failing test → `test` job fails.
- Integration: open a PR that changes a model without a migration → `makemigrations --check` fails.
- Happy path: clean PR → both jobs green.

**Verification:**
- Both jobs appear as checks on the PR and pass on the branch.

### U2. CD image publish workflow

**Goal:** Publish a tested, production-only image to GHCR from `master`.

**Requirements:** R4

**Dependencies:** U1

**Files:**
- Create: `.github/workflows/cd.yml`

**Approach:**
- `workflow_run` on CI completed for `master`, gated on `conclusion == success`; build with `REQUIREMENTS=requirements/base.txt`; tags `latest` + long SHA.

**Test scenarios:**
- Happy path: merge to `master` with green CI → image with SHA tag appears in GHCR.
- Error path: CI red on `master` → CD job skipped.

**Verification:**
- Package listed under the repo after the first merge.

### U3. Dependabot

**Goal:** Automated weekly update PRs.

**Requirements:** R5

**Dependencies:** None

**Files:**
- Create: `.github/dependabot.yml`

**Approach:**
- Ecosystems: pip (`/requirements`), github-actions, docker.

**Test expectation:** none -- declarative config.

**Verification:**
- Dependabot appears under repo Insights after merge.

### U4. Document the pipeline

**Goal:** Contributors know what CI enforces and how to reproduce it locally.

**Requirements:** R6

**Dependencies:** U1, U2

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

**Approach:**
- Short "CI/CD" section: jobs, what fails a PR, image location, and the recommendation to mark `test` and `docker-build` as required checks.

**Test expectation:** none -- documentation.

**Verification:**
- Docs describe the workflows as committed.

---

## System-Wide Impact

- **Unchanged invariants:** application code, settings, and Dockerfile are untouched.
- **Integration coverage:** first PR/merge is the real proof; local YAML validation cannot exercise the runners.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Open PRs add deps (e.g. browser automation) that need system packages in CI | Adjust the install step when a red run shows it |
| GHCR push denied by org package settings | Deferred check after first merge; documented |
| `workflow_run` only fires once `cd.yml` is on `master` | Expected; first verification is post-merge |

---

## Sources & References

- Related code: `config/settings/test.py`, `Dockerfile`, `docker-compose.yml`, `AGENTS.md`
