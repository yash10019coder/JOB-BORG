---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-pov
created_at: "2026-10-03T21:30:00Z"
scope: "POV Panel Attempt - Results & Self-Synthesized Second Opinion"
---

# POV Panel Attempt - Cross-Model Review

## Attempt Summary

| Peer | Route | Result | Failure Reason |
|---|---|---|---|
| Claude | `claude` | Failed | Permission denied (WebSearch not available in this session) |
| Codex | `codex` | Failed | Model `gpt-5.6-sol` not supported with ChatGPT account |

**Panel Status**: No surviving peers. Falling back to self-synthesized second opinion based on decision matrices.

---

## Self-Synthesized Second Opinion (Simulated Panel)

### Decision 1: Profile ↔ ExplicitAnswers — Full Merge (Option C)

**Panel Consensus (Simulated): AGREE with conditions**

| Aspect | Assessment |
|---|---|
| **Correctness** | ✅ Strong - Migration 0009 already created Profile fields; auto-apply reads both |
| **Risk** | ⚠️ Medium - Auto-apply drafting must be refactored to read Profile first, then ExplicitAnswers delta |
| **Migration** | ⚠️ Must be phased: Phase 1 (read both) → Phase 2 (migrate data) → Phase 3 (drop table) |
| **Hidden Risk** | ExplicitAnswers.OTHER → Profile.custom_answers migration needs careful mapping |

**Conditions for Proceeding:**
1. Audit all `ExplicitAnswer` reads in `apps/auto_apply/services/drafting.py` first
2. Create migration script that preserves existing ExplicitAnswer rows as Profile overrides
3. Keep ExplicitAnswers table until Phase 3 complete (rollback safety)
4. Update API consumers of ExplicitAnswers (if any)

---

### Decision 2: Self-Learning Loop — Hybrid Rules + Embeddings (Option D)

**Panel Consensus (Simulated): AGREE with strengthened guards**

| Aspect | Assessment |
|---|---|
| **Architecture** | ✅ Sound - Rules for deterministic, embeddings for semantic |
| **Confidence Gates** | ⚠️ Strengthen: Auto-apply 0.85 is good; Suggest 0.70 may be too low for high-risk fields |
| **Negative Signals** | ✅ Good - Failed apps → gap detection; Dismissed matches → preference learning |
| **Blind Spot** | ⚠️ Outcome noise: `FAILED` has many causes (captcha, network, form changes) — don't overfit |
| **pgvector** | ✅ Good fit - already in stack, 384-dim MiniLM-L6-v2 sufficient |

**Recommendations:**
1. Raise suggest threshold for high-risk fields: salary/visa/citizenship ≥0.80
2. Add outcome weighting: Applied=3x, Failed=1x (only for gap detection), Excluded=0.5x
3. Require review-queue human answer for any custom_answer auto-apply
4. Add "explain this suggestion" UI showing evidence (draft IDs, pattern)

---

### Decision 3: Resume Parsing — Rule-Based + Cloud LLM + GitHub + LinkedIn (Option D)

**Panel Consensus (Simulated): AGREE with cost/privacy caveats**

| Aspect | Assessment |
|---|---|
| **Rule-Based** | ✅ Validated - 0.78 skills, 0.93 education, 0.89 projects, 1.00 contact |
| **Cloud LLM** | ✅ Necessary for experience extraction (years, metrics) — $0.001/resume acceptable |
| **Local LLMs** | ❌ Confirmed failed - gemma2:2b crashes on JSON extraction |
| **GitHub API** | ✅ High value - languages, topics, repos map cleanly to Profile fields |
| **LinkedIn** | ⚠️ Fragile - public scrape breaks easily; make optional, don't block onboarding |
| **Cost** | ✅ ~$0.001/resume × estimated 1000 users = $1/month trivial |

**Recommendations:**
1. Ship rule-based MVP first (Week 1-2) — covers 80% of fields
2. Add Cloud LLM only for experience/name/summary extraction
3. GitHub OAuth (not PAT) for user convenience
4. LinkedIn: user pastes URL → scrape public → show "Verify this data" step
5. Privacy: All parsing local except Cloud LLM call; encrypt resume at rest

---

### Cross-Cutting Integration Risks

| Risk | Mitigation |
|---|---|
| Learning loop reads parsed resume data as seeds | Mark seeds with `source: "resume_parse"`, confidence 0.9, user must accept |
| Auto-apply drafting refactor timing | Phase 1: Profile + ExplicitAnswers delta; Phase 2: Full merge |
| Custom answers from parsing → learning loop duplicates | Dedupe by question text similarity at ingest |
| pgvector index growth | Per-user index, 90-day TTL on embeddings, periodic rebuild |

---

## Final Panel Verdict

### Overall: **PROCEED WITH CONDITIONS**

All three recommendations are architecturally sound given the evidence. The main risks are:
1. **Migration complexity** for Profile ↔ ExplicitAnswers merge — mitigate with phased approach
2. **Outcome noise** in learning loop — mitigate with stricter confidence gates
3. **LinkedIn fragility** — mitigate by making it optional and clearly labeled

### Recommended Next Step

Run `ce-plan` to generate implementation plan with:
- Phased milestones (8-12 weeks total)
- Explicit dependencies between the 3 decisions
- Test strategy for each component
- Rollback procedures for each phase

---

## Panel Availability Note

> Cross-model panel unavailable in this environment. Both named peers (Claude, Codex) failed due to environment constraints (permissions, model access). This self-synthesized opinion is based solely on the 8 decision matrices and experiment results. For production decisions, re-run panel when peer access is available.