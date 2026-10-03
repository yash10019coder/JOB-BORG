---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm + ce-pov
created_at: "2026-10-03T23:00:00Z"
scope: "Complete consolidated requirements for JobBorg Profile System overhaul"
---

# Consolidated Requirements: JobBorg Profile System Overhaul

## Problem Frame
Current state: Two separate pages (Profile + ExplicitAnswers) with overlapping fields, no learning from applications, no resume parsing. Users confused by Citizenship/Min Salary fields. Auto-apply fails on custom questions.

**Goal**: Single coherent profile system where user intent, parsed data, and learned patterns converge with clear provenance — enabling high-success auto-apply with minimal manual maintenance.

---

## Actors
| ID | Actor | Description |
|---|---|---|
| A1 | **Job Seeker** | Creates profile, manages auto-apply settings, reviews applications |
| A2 | **Auto-apply Engine** | Drafts/submits applications using profile data |
| A3 | **Learning Loop** | Analyzes applications, suggests profile improvements |
| A4 | **Resume Parser** | Extracts structured data from uploaded documents |

---

## Functional Requirements

### FR1: Unified Profile UI (Tabbed)
| ID | Requirement | Priority |
|---|---|---|
| FR1.1 | Three tabs: **Profile** (job search criteria) \| **Auto-apply Answers** (structured data for form filling) \| **Learning** (suggestions dashboard) | P0 |
| FR1.2 | Tab 1 (Profile): Target titles, tags, locations, remote preference, min salary (global fallback), resume upload, contact fields | P0 |
| FR1.3 | Tab 2 (Auto-apply Answers): Visa repeater, citizenship multi-select, 7 region salary dropdowns, custom answers, questions panel | P0 |
| FR1.4 | Tab 3 (Learning): Pending suggestions, applied updates history, learning dashboard charts | P1 |
| FR1.5 | Each Tab 2 field shows "Auto-apply will use: [derived value]" with override toggle | P0 |
| FR1.6 | Save Tab 1 → triggers rematch, redirects to recommendations. Save Tab 2 → updates Profile + AnswerBank, no redirect | P0 |

### FR2: Work Authorization by Country (Visa Repeater)
| ID | Requirement | Priority |
|---|---|---|
| FR2.1 | Per-country repeater: Country dropdown (252 ISO alpha-3) + Status dropdown (10 values) + Remove button | P0 |
| FR2.2 | Status vocabulary: `citizen`, `permanent_resident`, `work_authorized`, `requires_sponsorship`, `opt`, `cpt`, `h1b`, `tn_visa`, `other`, `unknown` | P0 |
| FR2.3 | Seeded from `Profile.visa_status_by_country` JSON dict | P0 |
| FR2.4 | Strict validation: unrecognized country/status = form error (no silent drops) | P0 |
| FR2.5 | Duplicate country = form error | P0 |
| FR2.6 | Derived visa-free countries: where status ∈ {citizen, permanent_resident, no_sponsorship_needed} | P0 |

### FR3: Citizenship (Passports Held)
| ID | Requirement | Priority |
|---|---|---|
| FR3.1 | Multi-select of 252 countries (ISO alpha-3), labeled "Countries whose passports you hold" | P0 |
| FR3.2 | Stored as `Profile.citizenship_countries` JSON list of alpha-3 codes | P0 |
| FR3.3 | Used for "Are you a citizen of X?" questions | P0 |
| FR3.4 | Help text explains purpose | P0 |

### FR4: Salary by Region + Global Fallback
| ID | Requirement | Priority |
|---|---|---|
| FR4.1 | 7 region dropdowns: US, CA, UK, AU, IN, SG, EU with region-specific bands | P0 |
| FR4.2 | Global fallback: `min_salary` (integer) + `salary_currency` selector (USD, INR, EUR, SGD, CAD, AUD, GBP) | P0 |
| FR4.3 | Resolution order: job's region dropdown → global `min_salary` (currency converted) → band conversion | P0 |
| FR4.4 | Band definitions per region (see salary bands table) | P0 |

### FR5: Custom Answers (Structured Q&A)
| ID | Requirement | Priority |
|---|---|---|
| FR5.1 | `Profile.custom_answers`: JSON list of `{question, answer, category, confidence, source, created_at}` | P0 |
| FR5.2 | Categories: `experience`, `technologies`, `relocation`, `projects`, `education`, `availability`, `compliance`, `visa_details`, `other` | P0 |
| FR5.2 | UI: Collapsible sections per category, inline add/edit/delete | P0 |
| FR5.3 | LLM matching at apply time: semantic similarity ≥0.75 returns answer | P0 |
| FR5.4 | Source tracking: `user`, `parsed`, `learned`, `override` | P0 |

### FR6: Application Questions Panel + Quick-Fill
| ID | Requirement | Priority |
|---|---|---|
| FR6.1 | Accordion panels per category showing unique questions from user's drafts (last 200) | P0 |
| FR6.2 | Each row: question text (truncated), count, last seen, quick-fill button | P0 |
| FR6.3 | Quick-fill → Confirm Modal: "Map to Salary (US)? [Accept] [Choose field]" | P0 |
| FR6.4 | Mapping rules: salary→region, auth/visa→visa repeater, sponsorship→requires_sponsorship, citizen→citizenship, location→target_locations/custom, experience→custom_answers | P0 |
| FR6.5 | On accept: target field highlights, toast confirms, question shows ✓ badge | P0 |
| FR6.6 | Cached per user, TTL 1 hour, invalidated on new draft | P0 |

### FR7: Learning Loop
| ID | Requirement | Priority |
|---|---|---|
| FR7.1 | Signals: explicit edits, application questions, outcomes, match quality, review queue edits | P0 |
| FR7.2 | Rule engine (deterministic): salary bands, visa sponsorship, citizenship questions, location patterns | P0 |
| FR7.3 | Embedding engine (semantic): pgvector + MiniLM-L6-v2, cluster custom questions, find consensus | P0 |
| FR7.5 | Risk gate: low-risk (custom_answers, confidence≥0.85) → auto-apply; high-risk (salary, visa, citizenship, locations) → suggest only | P0 |
| FR7.6 | Confidence scoring: count (30%), consistency (25%), recency (20%), outcome weight (15%), review-queue (10%) | P0 |
| FR7.7 | Three execution contexts: incremental hook (post-draft), daily batch (3 AM), on-demand ("Improve my profile") | P0 |
| FR7.8 | `ProfileSuggestion` model tracks every proposed change with evidence, confidence, status | P0 |
| FR7.9 | UI: Learning tab + inline hints + profile banner + weekly digest email | P0 |
| FR7.10 | Negative signals: failed apps → gap detection; dismissed matches → preference learning | P0 |
| FR7.11 | Privacy: `learning_enabled` flag, 90-day TTL on unresolved suggestions, GDPR export | P0 |

### FR8: Resume Parsing → Profile Integration
| ID | Requirement | Priority |
|---|---|---|
| FR8.1 | Rule-based parser (pdfplumber): contact, skills, education, projects (validated 0.78-1.00 F1) | P0 |
| FR8.2 | Cloud LLM (GPT-4o-mini, ~$0.001/resume): experience extraction (years, metrics), name, summary | P0 |
| FR8.3 | GitHub API (username/OAuth): languages → target_tags, topics → custom_answers, repos → portfolio_url | P0 |
| FR8.4 | LinkedIn: PDF upload only (no scrape) → same parser | P0 |
| FR8.5 | Onboarding wizard: Upload → Parse → GitHub → LinkedIn PDF → Review/Edit → Save | P0 |
| FR8.6 | Re-sync buttons: Resume / GitHub / LinkedIn → show diff → user accepts/rejects per field | P0 |
| FR8.7 | Field-level provenance: `source` (rule/llm/github/user) + user locks on re-sync | P0 |
| FR8.8 | Async onboarding: Celery tasks + WebSocket/polling progress UI | P0 |
| FR8.9 | Parsed data seeds learning loop: `source="resume_parse"`, confidence 0.9 | P0 |

### FR9: Provenance & Precedence Model
| ID | Requirement | Priority |
|---|---|---|
| FR9.1 | Single `AnswerBank` table: `profile, question_key, value, source, confidence, status, is_locked, created_at, updated_at` | P0 |
| FR9.2 | Precedence: User-locked > User-set > Learned > Parsed (with timestamps) | P0 |
| FR9.3 | `resolve_answer(profile, question)` service returns value + provenance | P0 |
| FR9.4 | Auto-apply uses resolver; Tab 2 shows derived + override toggle | P0 |
| FR9.5 | Learning loop writes to AnswerBank (learned); Parser writes to Profile (parsed) with provenance | P0 |

---

## Non-Functional Requirements

| ID | Requirement | Target |
|---|---|---|
| NFR1 | Auto-apply `unanswerable_required` rate | ≤12% (from ~18%) |
| NFR2 | Draft→Applied conversion rate | ≥15% improvement |
| NFR3 | Learning suggestion acceptance rate | ≥60% |
| NFR4 | Resume parse latency (rule-based) | <500ms |
| NFR5 | Resume parse latency (LLM) | <5s |
| NFR6 | Onboarding wizard completion | <3 min |
| NFR7 | Questions panel load time | <1s (cached) |
| NFR8 | Learning suggestion precision (auto-apply) | ≥99% (shadow mode first) |
| NFR9 | GDPR compliance: export, delete, consent | Full |

---

## Data Model Changes

### New Models
```python
# apps/accounts/models.py additions
class Profile(models.Model):
    # ... existing fields ...
    visa_status_by_country = JSONField(default=dict)      # {alpha3: status}
    citizenship_countries = JSONField(default=list)       # [alpha3, ...]
    salary_region_US = CharField(choices=US_BANDS, blank=True)
    salary_region_CA = CharField(choices=CA_BANDS, blank=True)
    salary_region_UK = CharField(choices=UK_BANDS, blank=True)
    salary_region_AU = CharField(choices=AU_BANDS, blank=True)
    salary_region_IN = CharField(choices=IN_BANDS, blank=True)
    salary_region_SG = CharField(choices=SG_BANDS, blank=True)
    salary_region_EU = CharField(choices=EU_BANDS, blank=True)
    min_salary = IntegerField(null=True)
    salary_currency = CharField(choices=CURRENCY_CHOICES, default="USD")
    custom_answers = JSONField(default=list)              # [{question, answer, category, confidence, source, created_at}]
    learning_enabled = BooleanField(default=True)

class AnswerBank(models.Model):
    profile = FK(Profile, related_name="answer_bank")
    question_key = CharField(max_length=128)      # e.g., "salary_region_US", "visa_status_USA"
    value = JSONField()
    source = CharField(choices=[("user", "User"), ("learned", "Learned"), ("parsed", "Parsed"), ("override", "Override")])
    confidence = FloatField(default=1.0)
    status = CharField(choices=[("active", "Active"), ("superseded", "Superseded")])
    is_locked = BooleanField(default=False)
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)

class ProfileSuggestion(models.Model):
    # ... as defined in FR7.8 ...

class QuestionEmbedding(models.Model):
    user = FK(User, related_name="question_embeddings")
    question_text = TextField()
    embedding = VectorField(dimensions=384)  # pgvector
    draft_id = IntegerField()
    answer_text = TextField()
    created_at = DateTimeField(auto_now_add=True)
```

### Migrations
1. Add `AnswerBank` table
2. Add `ProfileSuggestion` table
3. Add `QuestionEmbedding` table (pgvector)
4. Add `Profile.custom_answers`, `learning_enabled` fields
5. Migrate `ExplicitAnswer` rows → `AnswerBank` (preserve as overrides)
6. Drop `ExplicitAnswer` table (Phase 3)

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/profile/` | GET/POST | Tab 1 + Tab 2 unified form |
| `/profile/learn/` | POST | Trigger on-demand learning |
| `/profile/parse-resume/` | POST | Upload resume → async parse → returns job_id |
| `/profile/parse-status/<job_id>/` | GET | Poll parse progress |
| `/profile/re-sync/` | POST | Re-sync from Resume/GitHub/LinkedIn |
| `/profile/questions-panel/` | GET | Cached questions panel data |
| `/profile/suggestions/` | GET/POST | List/resolve suggestions |
| `/api/resolve-answer/` | POST | `resolve_answer(profile, question)` for auto-apply |

---

## Implementation Phases

### Phase 1: Foundation (Weeks 1-2)
- [ ] `AnswerBank` model + migration
- [ ] `resolve_answer()` service
- [ ] Update auto-apply drafting to use resolver
- [ ] Tabbed Profile UI (Tabs 1, 2, 3 skeleton)
- [ ] Visa repeater + citizenship multi-select + salary region dropdowns
- [ ] Rule-based resume parser (pdfplumber)

### Phase 2: Auto-apply Integration (Weeks 3-4)
- [ ] ExplicitAnswers migration → AnswerBank
- [ ] Override toggles in Tab 2
- [ ] Questions panel + quick-fill modal
- [ ] Cloud LLM resume parser (GPT-4o-mini)
- [ ] GitHub API enrichment

### Phase 3: Learning Loop (Weeks 5-6)
- [ ] `ProfileSuggestion` + `QuestionEmbedding` models
- [ ] Rule engine (salary, visa, citizenship, location)
- [ ] Embedding engine (pgvector + MiniLM-L6-v2)
- [ ] Incremental hook + daily batch + on-demand
- [ ] Learning Tab UI + inline hints + banner + email

### Phase 4: Resume Parsing + Onboarding (Weeks 7-8)
- [ ] Onboarding wizard (Resume → GitHub → LinkedIn PDF → Review)
- [ ] Re-sync buttons with diff UI
- [ ] Async Celery + WebSocket progress
- [ ] LinkedIn PDF upload (drop scrape)
- [ ] Parsed data → learning loop seeds

### Phase 5: Migration + Cleanup (Week 9)
- [ ] ExplicitAnswers → AnswerBank migration script
- [ ] Drop ExplicitAnswers table
- [ ] Remove ExplicitAnswers page/view
- [ ] Shadow mode learning loop (2 weeks)
- [ ] Enable auto-apply where precision >99%

---

## Test Scenarios

| Feature | Scenarios |
|---|---|
| Visa repeater | Add/remove countries, duplicate validation, status validation, seed from Profile |
| Citizenship | Multi-select, max 252, help text visible |
| Salary regions | 7 dropdowns work, global fallback converts currency, resolution order correct |
| Custom answers | CRUD per category, LLM match ≥0.75 returns answer |
| Questions panel | Loads from drafts, quick-fill modal maps correctly, cache invalidates |
| Learning loop | Rule suggestions at ≥0.85 auto-apply; embedding suggestions at ≥0.70 suggest; shadow mode logs |
| Resume parse | Rule-based F1 >0.75 on contact/skills/education/projects; LLM extracts years/metrics |
| Onboarding | Wizard completes <3 min; async progress works; re-sync shows diff |
| Provenance | AnswerBank precedence correct; resolver returns value + source; overrides work |

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MiniLM embeddings produce false positives | High | High | Shadow mode first; exact match priority; 99% precision gate |
| LinkedIn scrape banned | Medium | High | **Drop scrape** → LinkedIn PDF upload |
| LLM prompt injection via resume | Medium | High | Source span grounding; never parse visa/citizenship/salary |
| Migration data loss | Low | Critical | Phased migration; keep ExplicitAnswers until Phase 5 verified |
| Three writers conflict (user/parser/learner) | High | High | Provenance model + precedence + user locks |
| Learning loop stale answers | Medium | Medium | 90-day TTL on learned; 30-day on parsed |

---

## Success Criteria (30 Days Post-Launch)
1. `unanswerable_required` exclusion rate ≤12%
2. Draft→Applied conversion +15%
3. Learning suggestion acceptance ≥60%
4. Zero unwanted profile changes (high-risk fields always require approval)
5. Onboarding completion rate >80%
6. Resume parse accuracy (contact/skills/education/projects) >90% F1

---

## Open Questions (Resolved by Review)
| Question | Resolution |
|---|---|
| Profile ↔ ExplicitAnswers merge? | **Resolver layer** (AnswerBank), not schema merge |
| Learning auto-apply threshold? | **Exact match → RAG+LLM → shadow → 99% gate** |
| LinkedIn integration? | **PDF upload only**, no scrape |
| Resume parsing approach? | **LLM primary + rules fallback**, async onboarding |
| Provenance model? | **AnswerBank** with precedence: user-locked > user-set > learned > parsed |