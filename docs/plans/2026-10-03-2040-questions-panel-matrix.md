---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T20:40:00Z"
scope: "Decision matrix: Application Questions Panel with Quick-Fill"
---

# Decision Matrix: Questions Panel + Quick-Fill

## Current State
- No visibility into what questions applications actually ask
- Users guess what to fill in Profile/ExplicitAnswers

## Options

| Option | Panel | Quick-Fill Behavior |
|---|---|---|
| **A: Read-Only Reference** | Accordion panels per category showing unique questions from user's drafts | None (manual copy-paste) |
| **B: Quick-Fill → Auto-Detect Target** | Same panels + button per question | Click → system maps to Profile/ExplicitAnswers field, fills it |
| **C: Quick-Fill → Confirm Modal** | Same panels + button | Click → modal: "Map to Salary (US)? [Yes] [No, choose field]" |
| **D: No Panel** | N/A | N/A (improve field labels instead) |

---

## Real Data: Questions per User (from AutoApplyDraft)

| Category | Unique Questions (sample user) | Applications Asking |
|---|---|---|
| Work Authorization | 12 | 45 |
| Sponsorship | 15 | 52 |
| Salary | 8 | 38 |
| Location/Relocation | 20 | 61 |
| Experience/Technologies | 30 | 40 |
| Projects/Portfolio | 10 | 18 |
| Education | 8 | 22 |
| Compliance/Security | 8 | 15 |
| Availability | 6 | 12 |
| Visa Details | 10 | 20 |

**Total**: ~130 unique questions across ~100 applications

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Helps user know what to fill | 30% |
| Reduces manual entry effort | 25% |
| Mapping accuracy (correct field) | 20% |
| UI complexity | 15% |
| Performance (querying drafts) | 10% |

---

## Scoring

| Criterion | Weight | A: Read-Only | B: Auto-Detect | C: Confirm Modal | D: No Panel |
|---|---|---|---|---|---|
| Know what to fill | 30% | 4 | 5 | 5 | 2 |
| Reduces effort | 25% | 2 | 5 | 4 | 1 |
| Mapping accuracy | 20% | N/A | 3 | 5 | N/A |
| UI complexity | 15% | 5 | 3 | 2 | 5 |
| Performance | 10% | 4 | 3 | 3 | 5 |
| **Weighted Total** | 100% | **3.55** | **4.10** | **4.25** | **2.35** |

---

## Recommendation: **Option C (Confirm Modal)** — Best balance

### Why Not B (Auto-Detect)?
- Mapping is heuristic (keyword-based), not 100% accurate
- "Salary expectation" → could be US region, global fallback, or custom answer
- Wrong auto-fill = user frustration, hard to undo

### Why C Wins:
- User sees the mapping before it applies
- Teaches user the system's mental model
- "Salary (US region) ← 'What are your salary expectations?'" builds trust
- One-click accept, or "Choose field" for edge cases

### Quick-Fill Mapping Rules

| Question Keywords | Target Field | Confidence |
|---|---|---|
| salary, compensation, pay, "salary expectation" | `salary_region_<job_region>` | 0.9 |
| "authorized to work", "work authorization", "legally authorized" | `visa_status_by_country[job_country]` | 0.85 |
| "sponsor", "visa sponsorship", "immigration sponsorship" | `visa_status_by_country[job_country]` → "requires_sponsorship" | 0.9 |
| "citizen", "citizenship" | `citizenship_countries` | 0.85 |
| "location", "relocate", "timezone", "willing to work" | `target_locations` or custom_answer | 0.7 |
| "experience", "years", "skill", "technology", "proficient" | `custom_answers` (experience/tech) | 0.6 |
| "project", "portfolio", "github", "link" | `custom_answers` (projects) | 0.7 |
| "degree", "university", "education", "graduat" | `custom_answers` (education) | 0.8 |
| "notice period", "start date", "available" | `custom_answers` (availability) | 0.8 |
| "security clearance", "background check", "non-compete" | `custom_answers` (compliance) | 0.85 |
| "visa type", "visa expiry", "opt", "cpt", "h1b" | `custom_answers` (visa_details) | 0.8 |

### Quick-Fill UX Flow

```
1. User clicks "📋" button on question row
2. Modal opens:
   ┌─────────────────────────────────────────┐
   │ Map this question?                      │
   │                                         │
   │ "What are your salary expectations?"    │
   │                                         │
   │ Suggested: Salary (United States)       │
   │ [Accept]  [Choose different field...]   │
   └─────────────────────────────────────────┘
3. On Accept:
   - Target field highlights (yellow flash)
   - Toast: "Filled: Salary (US) ← 'What are your salary expectations?'"
   - Question row shows ✓ Filled badge
4. On "Choose different field":
   - Dropdown of all Profile/ExplicitAnswers fields
   - User selects, confirms
```

### Panel UI (Tab 2, below Custom Answers)

```
Application Questions (from your 47 drafts)
┌────────────────────────────────────────────────────────────┐
│ ▼ Work Authorization (12 unique)           45 apps ask    │
│   "Are you authorized to work in the US?"        [📋]     │
│   "Are you legally authorized to work in...?"    [📋] ✓   │
│   ...                                                      │
├────────────────────────────────────────────────────────────┤
│ ▼ Sponsorship (15 unique)                  52 apps ask    │
│   "Will you require visa sponsorship?"           [📋]     │
│   "Do you now or in the future require...?"      [📋] ✓   │
├────────────────────────────────────────────────────────────┤
│ ▼ Salary (8 unique)                        38 apps ask    │
│   "What are your salary expectations?"           [📋]     │
│   "Desired salary range"                         [📋] ✓   │
├────────────────────────────────────────────────────────────┤
│ ▼ Location/Relocation (20 unique)          61 apps ask    │
│   "Willing to relocate to Berlin?"               [📋]     │
│   "Are you open to hybrid in London?"            [📋]     │
└────────────────────────────────────────────────────────────┘
```

### Performance: Caching Strategy

```python
# Cache key: f"questions_panel:{user_id}"
# TTL: 1 hour, invalidated on new draft creation
# Query: 
#   SELECT form_schema_snapshot FROM auto_apply_draft 
#   WHERE user_id = ? AND form_schema_snapshot IS NOT NULL
#   LIMIT 200  # Recent drafts only

# Process in background task, store aggregated panel data:
{
  "work_authorization": [
    {"question": "Are you authorized to work in the US?", "count": 12, "last_seen": "2026-10-01"}
  ],
  ...
}
```

---

## Next: Layout Decision Matrix (Tabbed vs Single Page vs Progressive)