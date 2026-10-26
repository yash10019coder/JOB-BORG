---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-pov
created_at: "2026-10-03T22:00:00Z"
scope: "Synthesized cross-model review: Claude + AGY/Gemini on 3 architectural decisions"
---

# Synthesized Cross-Model Review: 3 Architectural Decisions

## Panel Composition
| Reviewer | Status | Key Strength |
|---|---|---|
| **Claude** (via `claude -p`) | ✅ Success | Deep schema + provenance + legal analysis |
| **AGY/Gemini** (via `agy -p`) | ✅ Success | RAG/LLM architecture + operational pragmatism |
| **Codex** | ❌ Failed | Terminal/syntax issues |

---

## Decision 1: Profile ↔ ExplicitAnswers — Full Merge

### Concerns (Both Agree)
| Concern | Claude | AGY |
|---|---|---|
| **Provenance/audit loss** | ✅ "Override toggles imply second layer; each answer needs source, confidence, status, lock" | ✅ "Schema bloat and mapping rigidity; ATS questions infinitely varied" |
| **Temporal drift** | ⚠️ Implied | ✅ "Answers change over time (salary, notice period) — need timestamps" |
| **Schema rigidity** | ⚠️ Implied | ✅ "Single Profile answer doesn't map to varying ATS formats" |

### Blind Spots Identified
- **Claude**: Greenhouse custom questions unbounded → partial merge anyway; Profile edits trigger debounced rematch unless scoped
- **AGY**: Temporal drift — answers correct 6 months ago wrong today

### Alternatives Proposed
| Reviewer | Alternative |
|---|---|
| **Claude** | **Merge at service/UI layer, not schema**: Keep typed facts on Profile. Keep slim `Answer` table with `profile, question_key, value, source, confidence, status, is_locked`. Single `resolve_answer(profile, question)` with facts as defaults, rows as sparse overrides. |
| **AGY** | **Keep `Profile` for canonical identity**, use separate `UserAnswerBank` for ATS long tail. Store semantic question, answer, original ATS field type, `last_updated`. |

### **Synthesis Verdict**: ⚠️ **Don't merge at schema level** — merge at resolver layer. Keep `Profile` as canonical facts, `AnswerBank` for overrides with provenance.

---

## Decision 2: Self-Learning Loop — Hybrid Rules + Embeddings

### Concerns (Both Agree)
| Concern | Claude | AGY |
|---|---|---|
| **Embedding quality** | ✅ "MiniLM-L6 weak on negation, numbers, regions; 0.85 threshold uncalibrated" | ✅ "MiniLM too weak for nuanced employment QA; scores 'need visa' ≈ 'have visa'" |
| **Risk assignment** | ✅ "Risk by field is wrong — should be by question semantics (veteran status, criminal history, notice period = high-risk)" | ✅ Implied |
| **Learning mechanism** | ✅ "No ground truth/eval plan; three writers share state (incremental, batch, on-demand)" | ✅ "How does system 'learn' from user override? Updating vectors requires fine-tuning, not just new rows" |
| **Staleness** | ✅ "Learned answers go stale (salary) or company-specific; no expiration" | ⚠️ Implied |

### Blind Spots Identified
- **Claude**: No ground truth, biased feedback (only questions users fix), three writers share state, torch dependency in workers
- **AGY**: "How does system learn from override?" — vector update ≠ learning; pgvector is just index

### Alternatives Proposed
| Reviewer | Alternative |
|---|---|
| **Claude** | **Exact matching first**: Canonicalize/hash question text + option set. Auto-fill only on exact normalized match. Embeddings only for retrieval → propose candidates, confirm with user first N times. Shadow mode first (log vs actual edits), enable auto-apply only where precision >99%. |
| **AGY** | **RAG + LLM**: Use pgvector for retrieval of similar past Q/A. Pass new question + retrieved history + canonical Profile to LLM (GPT-4o-mini) for final logical decision. |

### **Synthesis Verdict**: ⚠️ **Don't auto-apply from embeddings** — use exact match → RAG+LLM retrieval → human-in-the-loop for first N confirmations. Shadow mode first.

---

## Decision 3: Resume Parsing — Rule-Based + Cloud LLM + GitHub + LinkedIn

### Concerns (Both Agree)
| Concern | Claude | AGY |
|---|---|---|
| **LinkedIn scrape** | ✅ "Violates ToS, brittle, invites bans, liability for user data" | ✅ "Massive operational liability; IPs banned, feature breaks constantly" |
| **Cloud LLM PII** | ✅ "Needs consent, DPA, zero-retention for EU users" | ⚠️ Implied |
| **Parsed data overwrites user data** | ✅ "Re-sync needs field-level provenance + user locks; prompt injection risk" | ✅ "Conflict resolution chaos; latency in onboarding wizard" |
| **Validation scope** | ✅ "0.78–1.00 F1 likely from small hand-picked set; may not hold on real resumes" | ⚠️ Implied |

### Blind Spots Identified
- **Claude**: Parsed data overwrites user data; resume text = untrusted LLM input (prompt injection); never let parsing set visa/citizenship/salary
- **AGY**: Data conflict resolution (rule says Python, GitHub says JS, LLM says Java); onboarding latency (sync calls → user abandonment)

### Alternatives Proposed
| Reviewer | Alternative |
|---|---|
| **Claude** | **LLM primary, rules fallback**: Single structured-output LLM call for all fields (~$0.001). Validate with regexes. Require source span grounding, drop if not grounded. Drop LinkedIn scrape → "upload LinkedIn PDF export". GitHub public API with username only (no OAuth). |
| **AGY** | **Drop LinkedIn scrape** → "upload LinkedIn PDF export". **Go fully async**: Upload → Celery tasks → WebSocket/polling populate UI. Track `source` per field, user picks winner on conflict. |

### **Synthesis Verdict**: ✅ **Rule-based + Cloud LLM + GitHub is solid** but:
1. **Drop LinkedIn scrape** → LinkedIn PDF upload
2. **Async onboarding** with Celery + WebSocket
3. **Field-level provenance** + user locks on re-sync
4. **LLM primary** for experience extraction, rules fallback

---

## Cross-Cutting Synthesis (Both Identified)

> **"All three decisions have the user, the parser and the learner writing to the same fields, and none of them defines precedence. A single provenance and precedence model (user-locked > user-set > learned > parsed, with timestamps) is the real architectural decision. Settle it before Decision 1's migration, because 1, 2 and 3 all depend on it."** — **Claude**

> **"All three decisions have the user, the parser and the learner writing to the same fields... The real architectural decision is a single provenance and precedence model... Settle it before Decision 1's migration."** — **AGY (similar)**

### **Unified Provenance & Precedence Model**

| Priority | Source | Lockable | TTL |
|---|---|---|---|
| 1 | **User-locked** (explicit save) | ✅ | Never |
| 2 | **User-set** (Profile tab edit) | ✅ | Never |
| 3 | **Learned** (high-confidence auto-applied) | ❌ | 90 days |
| 4 | **Parsed** (resume/GitHub) | ❌ | 30 days |

---

## Updated Recommendations Summary

| Decision | Original | Revised (Post-Review) |
|---|---|---|
| **1. Profile ↔ ExplicitAnswers** | Full schema merge | **Merge at resolver layer** — keep `AnswerBank` table with provenance; `resolve_answer()` service |
| **2. Learning Loop** | Hybrid rules + embeddings auto-apply | **Exact match → RAG+LLM retrieval → shadow mode → 99% precision gate** |
| **3. Resume Parsing** | Rule + LLM + GitHub + LinkedIn | **LLM primary + rules fallback; async onboarding; drop LinkedIn scrape; field-level provenance** |

---

## Next Step

Run `ce-plan` with these revised decisions as settled constraints to generate the implementation plan.