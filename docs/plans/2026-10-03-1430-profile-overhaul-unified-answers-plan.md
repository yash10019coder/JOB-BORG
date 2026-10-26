---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T14:30:00Z"
scope: "Full profile overhaul unifying Profile + ExplicitAnswers into tabbed UI with Application Questions panel"
---

# Goal Capsule

**Objective**: Replace the current two-page Profile + ExplicitAnswers setup with a single tabbed form ("Profile" | "Auto-apply Answers") that eliminates field overlap, adds an Application Questions panel with quick-fill, and derives structured custom answers from real job-application questions so the LLM can answer them.

**Active Area**: Profile form + ExplicitAnswers form + AutoApplyDraft question analysis. Surrounding areas (matching, recommendations, job ingestion) are contextual only.

**Success Criteria**:
1. User completes both tabs in one session without field confusion
2. Auto-apply drafts show fewer "unanswerable_required" exclusions
3. Custom questions from applications are answered from structured user data (not free-text "Other")

---

# Product Contract

## 1. Unified Tabbed Form

### Tab 1: Profile (job-search criteria)
- Target titles, tags, locations (existing)
- Remote preference, min salary (global fallback — see §3), active toggle
- Resume upload, contact fields (existing)

### Tab 2: Auto-apply Answers (structured data for form filling)
**Sections**:
1. **Work Authorization by Country** — per-country repeater (country dropdown + status dropdown + remove), seeded from `Profile.visa_status_by_country`
2. **Citizenship (Passports Held)** — multi-select of 252 countries (ISO alpha-3), labeled "Select every country whose passport you hold"
3. **Salary by Region** — 7 region-specific dropdowns (US, CA, UK, AU, IN, SG, EU) using existing salary bands; replaces single `min_salary`
4. **Global Min Salary (fallback)** — single USD field used only when region-specific not set
5. **Custom Answers** — structured Q&A pairs (named question + answer), derived from application questions analysis (§4)
6. **Application Questions Panel** — read-only reference + quick-fill (§5)

**Derivation Display**: Each field shows "Auto-apply will use: [derived value]" with an override toggle. When user edits a Profile field (Tab 1), the derived value in Tab 2 updates live.

## 2. Citizenship = Passports Held

- **Mental model**: "Countries whose passports you hold" — used for "Are you a citizen of X?" questions
- **UI**: `SelectMultiple` with `size=8`, 252 choices (ISO alpha-3), help text explains purpose
- **Storage**: `Profile.citizenship_countries` (JSON list of alpha-3 codes)
- **Mapping to applications**: When application asks "Are you a US citizen?", system checks if `USA` in list

## 3. Salary: Region Dropdowns + Global Fallback

- **Region dropdowns** (already implemented): `salary_region_US`, `salary_region_CA`, `salary_region_UK`, `salary_region_AU`, `salary_region_IN`, `salary_region_SG`, `salary_region_EU`
- **Global fallback**: `min_salary` (integer, USD) — used only when no region-specific value set for the job's region
- **Auto-apply resolution order**: job's region dropdown → global `min_salary` → band conversion → ExplicitAnswer `salary_expectation`
- **Removed**: single `min_salary` as primary; hidden JSON `salary_by_region` blob

## 4. Structured Custom Answers from Application Questions

### Data Source
Query `AutoApplyDraft.form_schema_snapshot` across all user drafts (drafted, stale, applied, failed, excluded) to extract custom (non-standard) questions.

### Categorization Pipeline
1. **Extract** all field labels from `form_schema_snapshot.fields`
2. **Filter out** standard fields (first/last name, email, phone, resume, LinkedIn, GitHub, cover letter, country)
3. **Cluster** remaining by semantic similarity (embedding + k-means or keyword rules)
4. **Label clusters**: e.g., "Years of experience", "Specific technology experience", "Relocation willingness", "Project portfolio", "Non-compete/IP agreements", "Security clearance", "Shift/overtime availability"
5. **Present to user** as suggested custom answers with "Add to my answers" button

### User Model
```python
# Profile model addition
custom_answers = models.JSONField(default=list, blank=True)
# [{"question": "Years of Python experience", "answer": "5 years", "category": "experience"}, ...]
```

### LLM Integration
- Custom answers included in drafting context alongside Profile + ExplicitAnswers
- LLM matches application question to closest custom answer by semantic similarity
- Falls back to "needs_review" if no match above threshold

## 5. Application Questions Panel with Quick-Fill

### Panel Content
Grouped accordion sections (collapsible):
- **Work Authorization** (e.g., 12 unique questions across your applications)
- **Sponsorship** (15 unique)
- **Salary Expectation** (8 unique)
- **Location / Relocation** (20 unique)
- **Experience / Technologies** (30 unique)
- **Other Custom** (25 unique)

Each row shows:
- Question text (truncated, expandable)
- Count of applications asking it
- **Quick-fill button** → auto-detects target field and fills it

### Quick-Fill Mapping Rules
| Question Pattern | Target Field |
|---|---|
| "salary", "compensation", "pay" | `salary_region_<job_region>` or global `min_salary` |
| "authorized", "work authoriz", "legally authoriz" | Visa repeater row for job's country |
| "sponsor", "visa sponsor", "immigration sponsor" | Visa repeater row (sets status to "requires_sponsorship") |
| "citizen", "citizenship" | Citizenship multi-select |
| "location", "relocate", "timezone", "country" | Target locations or custom answer |
| "experience", "years", "skill", "technology" | Custom answers → new Q&A pair |
| "non-compete", "IP agreement", "security clearance" | Custom answers → new Q&A pair |

### Quick-Fill UX
1. User clicks quick-fill button on a question
2. System shows toast: "Filled: Salary (US region) ← 'What are your salary expectations?'"
3. Target field highlights briefly
4. User can undo or edit

## 6. ExplicitAnswers Page → Derived View

- **Remove** separate ExplicitAnswers form page
- **Tab 2** shows "Auto-apply will use:" for each category:
  - Work Authorization → derived from visa repeater (per-country)
  - Sponsorship → derived from visa status (per-country)
  - Salary Expectation → derived from salary region dropdowns
  - Custom Answers → derived from structured custom answers
- **Override toggle** per category: when on, shows editable field (same as current ExplicitAnswers form)
- **Save** writes to `ExplicitAnswer` rows (overrides) + `Profile` structured fields

## 7. Migration & Backward Compatibility

- **Data migration**: Existing `ExplicitAnswer` rows → Tab 2 overrides (preserved)
- **Profile.min_salary** retained as global fallback
- **Profile.salary_by_region** (JSON) already populated from region dropdowns
- **Profile.visa_status_by_country** and **citizenship_countries** unchanged
- **ExplicitAnswer.OTHER** category → migrated to `Profile.custom_answers` if non-empty

---

# How This Work Fits Together

| Area | Relationship |
|---|---|
| Auto-apply drafting | Consumes unified Profile + overrides; custom answers reduce "needs_review" |
| Matching | Unchanged — uses Profile target locations/titles/tags |
| Job ingestion | Unchanged |
| Classification | Unchanged |

---

# Open Questions for Planning

1. **Embedding model for question clustering** — use existing `pgvector` + sentence-transformers, or keyword rules first?
2. **Quick-fill mapping confidence** — auto-fill only above threshold, else show modal?
3. **Custom answers UI** — inline editable table in Tab 2, or separate modal?
4. **Performance** — Application Questions panel queries all user drafts; cache or paginate?