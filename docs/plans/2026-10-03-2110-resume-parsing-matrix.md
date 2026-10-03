---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T21:10:00Z"
scope: "Decision matrix: Resume Parsing → Profile Integration"
---

# Decision Matrix: Resume Parsing → Profile Integration

## Current State
- Resume upload exists (`Profile.set_resume()`)
- No parsing - file stored as PDF only
- job-agent has `resume_inventory.json` schema + tailoring logic

## Options

| Option | Parsing Trigger | Output Destination |
|---|---|---|
| **A: Onboarding Wizard** | First resume upload → parse → review → save to Profile | Profile fields + custom_answers |
| **B: Re-sync Button** | User clicks "Refresh from Resume" anytime | Merges with existing Profile |
| **C: Background Re-parse** | Periodic re-parse of uploaded resume | Updates Profile silently |
| **D: Resume + GitHub + LinkedIn** | Onboarding: all three sources → unified parse → review | Profile + custom_answers + portfolio |

---

## Experiment Results (from 5 test resumes)

| Parser | Contact | Skills | Experience | Education | Projects | Name | Summary |
|---|---|---|---|---|---|---|---|
| Rule-Based | 1.00 | 0.78 | 0.20 | 0.93 | 0.89 | 0.00 | 0.00 |
| Local LLM (gemma2:2b) | Failed - crashes, ANSI output | | | | | | |
| Cloud LLM (GPT-4o-mini) | Not tested | | | | | | |

**Rule-based strengths**: Contact, Education, Projects, Skills (keyword match)
**Rule-based gaps**: Experience extraction (section detection), Name (header parsing), Summary

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Field coverage (Profile completeness) | 25% |
| User trust (review before save) | 20% |
| Implementation effort | 15% |
| Ongoing value (re-sync) | 15% |
| GitHub/LinkedIn enrichment | 15% |
| Privacy (local vs cloud) | 10% |

---

## Scoring

| Criterion | Weight | A: Onboarding | B: Re-sync | C: Background | D: All Sources |
|---|---|---|---|---|---|
| Field coverage | 25% | 4 | 4 | 3 | 5 |
| User trust | 20% | 5 | 4 | 2 | 4 |
| Implementation | 15% | 4 | 3 | 3 | 2 |
| Ongoing value | 15% | 2 | 5 | 4 | 5 |
| GitHub/LinkedIn | 15% | 2 | 3 | 2 | 5 |
| Privacy | 10% | 5 | 4 | 3 | 3 |
| **Weighted Total** | 100% | **3.70** | **3.85** | **2.95** | **4.10** |

---

## Recommendation: **Option D (All Sources: Resume + GitHub + LinkedIn)** with **Onboarding + Re-sync**

### Why D Wins
- Maximum field coverage from multiple sources
- GitHub API provides: languages, repos, topics → skills, projects, portfolio_url
- LinkedIn (scrape/API): headline, location, skills → headline, target_locations, target_tags
- Onboarding wizard ensures user reviews before save
- Re-sync button keeps profile current

---

## Implementation Phases

### Phase 1: Rule-Based Resume Parser (Week 1-2)
- Input: PDF/DOCX upload
- Output: Partial `resume_inventory.json` → maps to Profile fields
- Fields covered: contact, skills, education, projects
- Gaps: experience section, name, summary

### Phase 2: Cloud LLM Enhancement (Week 3-4)
- OpenRouter GPT-4o-mini (~$0.001/resume)
- Focus: experience extraction (years per skill, metrics), name, summary
- Merge: LLM wins on conflicts with rule-based
- Fallback: if API unavailable, use rule-based only

### Phase 3: GitHub API Enrichment (Week 5)
- User provides GitHub username or OAuth
- Fetch: repos, languages, topics, starred
- Map: languages → `target_tags`, topics → `custom_answers`, repos → `portfolio_url` + projects

### Phase 4: LinkedIn Enrichment (Week 6)
- Option A: User pastes LinkedIn URL → scrape public profile
- Option B: LinkedIn OAuth (complex, restricted)
- Map: headline → `headline`, location → `target_locations`, skills → `target_tags`

### Phase 5: Onboarding Wizard + Re-sync (Week 7-8)
```
Step 1: Upload Resume (PDF/DOCX)          → Parse
Step 2: (Optional) GitHub username         → Enrich
Step 3: (Optional) LinkedIn URL            → Enrich
Step 4: Review & Edit Parsed Data          → User confirms
Step 5: Save to Profile                     → Profile + custom_answers
```

### Re-sync Button (Profile Page)
```
[Refresh from Resume] [Refresh from GitHub] [Refresh from LinkedIn]
```
- Re-runs parser/enrichment
- Shows diff: "We found: 3 new skills, 1 updated project. Keep changes?"
- User accepts/rejects per field

---

## Field Mapping: resume_inventory.json → JobBorg Profile

| JobBorg Field | Source | Priority |
|---|---|---|
| `full_name` | contact.name | Rule-based |
| `headline` | lane_profiles.*.headline OR contact.name + top role | LLM |
| `target_titles` | lane_profiles.*.title, roles.*.title | Rule |
| `target_tags` | skills.*.items + GitHub languages | Rule + GitHub |
| `target_locations` | contact.location, roles.*.location | Rule + LinkedIn |
| `custom_answers` (experience) | roles.*.bullets (years per skill) | LLM |
| `custom_answers` (technologies) | roles.*.bullets.skills + GitHub topics | LLM + GitHub |
| `custom_answers` (projects) | projects.*.bullets + GitHub repos | LLM + GitHub |
| `custom_answers` (education) | education.* | Rule |
| `portfolio_url` | contact.github, projects.*.url | Rule + GitHub |
| `github_url` | contact.github | Rule |
| `linkedin_url` | contact.linkedin | Rule + LinkedIn |

---

## Onboarding Wizard UX

```
┌─────────────────────────────────────────────────────────────┐
│  Build Your Profile (3 steps)                               │
├─────────────────────────────────────────────────────────────┤
│  Step 1: Resume                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Drag & drop PDF/DOCX or click to browse             │   │
│  │ [Choose File]  resume.pdf  ✓ Parsed successfully    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  Step 2: GitHub (optional)                                  │
│  GitHub username: [yash10019coder]  [Fetch]  ✓ 42 repos    │
│                                                             │
│  Step 3: LinkedIn (optional)                                │
│  LinkedIn URL: [linkedin.com/in/yash10019coder] [Fetch] ✓  │
│                                                             │
│  [Continue to Review]                                       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Review Parsed Data                                         │
├─────────────────────────────────────────────────────────────┤
│  ✓ Full Name: Yash Verma                    [Edit]          │
│  ✓ Headline: Backend Engineer | Distributed Systems...     │
│  ✓ Target Titles: Backend Engineer, Platform Engineer      │
│  ✓ Target Tags: Java, Go, Python, Kubernetes, AWS, GCP...  │
│  ✓ Target Locations: Bangalore, Mumbai, Remote             │
│  ✓ Min Salary: 120000 USD                                   │
│  ✓ Skills: 32 extracted (24 from resume, 8 from GitHub)    │
│  ✓ Experience: 3 roles, 12 custom answers generated        │
│  ✓ Projects: 3 projects + 12 GitHub repos                  │
│  ✓ Education: IIIT Lucknow, B.Tech IT                      │
│  ✓ Portfolio: github.com/yash10019coder                     │
│                                                             │
│  [← Back]    [Save Profile]                                 │
└─────────────────────────────────────────────────────────────┘
```

---

## Learning Loop Integration

| Parsed Data | Learning Loop Role |
|---|---|
| Skills from resume/GitHub | High-confidence seeds for `target_tags` |
| Experience years per tech | High-confidence seeds for `custom_answers` (experience) |
| Projects from resume/GitHub | Seeds for `custom_answers` (projects) + `portfolio_url` |
| Education | Seeds for `custom_answers` (education) |
| **All parsed data** | Marked as `source: "resume_parse"` in ProfileSuggestion evidence, confidence 0.9 |

---

## Privacy

- Resume PDF: stored encrypted, parsed locally (rule) or via API (LLM)
- GitHub: OAuth token stored encrypted, only public data fetched unless user grants private repo access
- LinkedIn: only public profile scraped, no credentials stored
- Opt-out: User can delete parsed data anytime, disable re-sync

---

## Summary: Complete Decision Matrix Index

| # | Decision | Recommendation | Plan File |
|---|---|---|---|
| 1 | Profile ↔ ExplicitAnswers | Full Merge (Option C) | 2000-profile-explicitanswers-matrix.md |
| 2 | Citizenship Model | Passports Held (Option A) | 2010-citizenship-model-matrix.md |
| 3 | Salary Model | Global + Region Overrides (Option B) | 2020-salary-model-matrix.md |
| 4 | Custom Answers | Structured Q&A Pairs (Option A) | 2030-custom-answers-matrix.md |
| 5 | Questions Panel | Confirm Modal Quick-Fill (Option C) | 2040-questions-panel-matrix.md |
| 6 | Layout | Tabbed Profile \| Auto-apply (Option B) | 2050-layout-matrix.md |
| 7 | Learning Loop | Hybrid Rules + Embeddings (Option D) | 2100-learning-loop-matrix.md |
| 8 | Resume Parsing | All Sources Onboarding + Re-sync (Option D) | 2110-resume-parsing-matrix.md |

---

## Next: Consolidated Unified Plan (ce-plan)