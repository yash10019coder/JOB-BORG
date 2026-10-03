---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T20:00:00Z"
scope: "Decision matrix: Profile ↔ ExplicitAnswers relationship"
---

# Decision Matrix: Profile ↔ ExplicitAnswers Relationship

## Options

| Option | Description |
|---|---|
| **A: Profile = Source of Truth** | Profile fields drive auto-apply; ExplicitAnswers stores only user overrides (delta). Auto-apply reads Profile + applies ExplicitAnswers delta. |
| **B: ExplicitAnswers = Source for Auto-Apply** | Auto-apply reads ExplicitAnswers exclusively. Profile fields (visa, citizenship, salary) only for matching/recommendations. Two separate models. |
| **C: Full Merge** | Eliminate ExplicitAnswers table. All auto-apply answers live on Profile as structured fields. ExplicitAnswers page becomes a Profile view. |
| **D: Derivation with Divergence (Current "Link")** | Profile → derives ExplicitAnswers. User can edit ExplicitAnswers directly. On Profile save, prompt to sync. Both models coexist. |

---

## Evaluation Criteria (Weights)

| Criterion | Weight | Rationale |
|---|---|---|
| **Auto-apply correctness** | 25% | Wrong answers = failed applications |
| **User mental model clarity** | 20% | "Where do I edit my salary?" |
| **Implementation complexity** | 15% | Code paths, migrations, tests |
| **Data integrity** | 15% | No stale/conflicting data |
| **Flexibility for custom questions** | 10% | Future: arbitrary Q&A pairs |
| **Migration risk** | 10% | Existing ExplicitAnswer rows |
| **Learning loop integration** | 5% | Self-learning writes where? |

---

## Scoring (1-5 per criterion)

| Criterion | Weight | A: Profile=Truth | B: ExplicitAnswers=Truth | C: Full Merge | D: Derivation+Divergence |
|---|---|---|---|---|---|
| Auto-apply correctness | 25% | 5 | 4 | 5 | 3 |
| User mental model | 20% | 4 | 3 | 5 | 2 |
| Implementation complexity | 15% | 4 | 3 | 5 | 2 |
| Data integrity | 15% | 5 | 3 | 5 | 2 |
| Custom questions flexibility | 10% | 3 | 4 | 4 | 4 |
| Migration risk | 10% | 3 | 5 | 2 | 4 |
| Learning loop integration | 5% | 5 | 2 | 5 | 3 |
| **Weighted Total** | 100% | **4.50** | **3.45** | **4.65** | **2.70** |

---

## Detailed Analysis

### Option A: Profile = Source of Truth ⭐ Strong Contender

**Pros:**
- Single source of truth for structured data
- Auto-apply logic simple: read Profile, apply ExplicitAnswers delta
- Learning loop writes to Profile directly
- Clear ownership: Profile owns structured, ExplicitAnswers owns overrides

**Cons:**
- ExplicitAnswers table becomes sparse (only overrides)
- Need derivation logic for each field type
- "Other" free-text doesn't map cleanly

**Best for:** When structured data is primary and overrides are rare.

---

### Option B: ExplicitAnswers = Source for Auto-Apply

**Pros:**
- Auto-apply code unchanged (already reads ExplicitAnswers)
- Free-text "Other" fits naturally
- Profile stays focused on matching criteria

**Cons:**
- Two sources of truth for visa/salary/citizenship
- Sync logic complex: Profile → ExplicitAnswers derivation must run on every Profile save
- User confusion: "I updated Profile but auto-apply still uses old value"
- Learning loop: writes to ExplicitAnswers? Profile? Both?

**Best for:** Minimal change to auto-apply pipeline.

---

### Option C: Full Merge ⭐ Highest Score

**Pros:**
- Single model, single source of truth
- No derivation/sync logic needed
- ExplicitAnswers page = Profile view (Tab 2)
- Learning loop writes to Profile.custom_answers
- Custom questions fit naturally as `custom_answers` JSON
- Simplest user mental model: "One profile, two tabs"

**Cons:**
- Migration: ExplicitAnswer rows → Profile fields (one-time)
- Auto-apply drafting must read Profile instead of ExplicitAnswers
- `other` free-text → structured `custom_answers` (schema change)
- Profile model gets larger

**Best for:** Long-term maintainability, user clarity.

---

### Option D: Derivation with Divergence (Current)

**Pros:**
- Preserves both models as-is
- Minimal immediate code change

**Cons:**
- **Worst data integrity**: two sources diverge silently
- **Worst user model**: "Which one does auto-apply use?"
- **Worst learning loop**: where does learned data go?
- Sync prompts annoy users ("Update saved answers?")

**Best for:** Only as temporary state during migration.

---

## Recommendation: **Option C (Full Merge)** with staged migration

### Migration Path

```
Phase 1 (Now): Add Profile.custom_answers, salary_region_*, keep ExplicitAnswers
Phase 2: Auto-apply reads Profile + ExplicitAnswers delta (Option A)
Phase 3: Migrate ExplicitAnswer rows → Profile fields, drop ExplicitAnswers table
Phase 4: ExplicitAnswers page = Profile Tab 2 view
```

### Auto-Apply Drafting Resolution Order (Phase 2)

```python
def get_answer(user, category):
    # 1. Explicit override (ExplicitAnswer row)
    override = ExplicitAnswer.objects.filter(user=user, category=category).first()
    if override:
        return override.answer_text, "override"
    
    # 2. Derived from Profile
    if category == "work_authorization":
        return derive_work_auth(user.profile), "profile"
    elif category == "sponsorship":
        return derive_sponsorship(user.profile), "profile"
    elif category == "salary_expectation":
        return derive_salary_band(user.profile), "profile"
    elif category == "other":
        return user.profile.custom_answers.get("other", ""), "profile"
    
    return "", "none"
```

### Learning Loop Writes

| Learned Data | Destination |
|---|---|
| Custom answers (skills, experience) | `Profile.custom_answers` |
| Salary preferences | `Profile.salary_region_*` |
| Visa/citizenship gaps | `Profile.visa_status_by_country`, `Profile.citizenship_countries` (as suggestions) |
| Target locations/titles/tags | `Profile.target_locations`, etc. (as suggestions) |

---

## Open Questions

1. **Auto-apply drafting refactor**: How much code reads `ExplicitAnswer` today? (Need code audit)
2. **Migration script**: One-time or phased? (Phased safer)
3. **Custom answers schema**: Free-text "Other" → structured `[{question, answer, category}]`?
4. **Backward compat**: API consumers of ExplicitAnswers?

---

## Next: Citizenship Mental Model Decision Matrix