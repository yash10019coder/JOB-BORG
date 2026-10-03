---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T16:30:00Z"
scope: "Decision matrix for resume/GitHub/LinkedIn parsing approach"
---

# Decision Matrix: Resume Parsing Approach

## Options Compared

| Dimension | Rule-Based (pdfplumber + regex) | Local LLM (Ollama: Llama 3.1 8B) | Cloud LLM (OpenRouter: GPT-4o-mini) | Hybrid (Rules + Local LLM) |
|---|---|---|---|---|
| **Accuracy: Structured fields** (name, email, phone, location) | ★★★★★ | ★★★★☆ | ★★★★★ | ★★★★★ |
| **Accuracy: Skills extraction** | ★★★☆☆ | ★★★★☆ | ★★★★★ | ★★★★☆ |
| **Accuracy: Experience bullets** (years, metrics) | ★★☆☆☆ | ★★★★☆ | ★★★★★ | ★★★★☆ |
| **Accuracy: Projects/GitHub** | ★★☆☆☆ | ★★★☆☆ | ★★★★☆ | ★★★☆☆ |
| **Latency (per resume)** | ~500ms | ~3-8s (GPU) / ~30s (CPU) | ~2-5s | ~1-2s (rules) + ~3s (LLM) |
| **Cost per resume** | $0 | $0 (self-hosted) | ~$0.001-0.005 | ~$0.001 |
| **Privacy** | ✅ Fully local | ✅ Fully local | ❌ Data to API | ✅ Mostly local |
| **Infrastructure** | None | GPU server / Ollama | API key only | GPU server |
| **Maintenance** | Regex updates | Model updates | API versioning | Both |
| **Job-agent compatibility** | Low (custom schema) | High (can output job-agent schema) | High | High |

## Scoring (1-5 per dimension, weights in parentheses)

| Dimension | Weight | Rule-Based | Local LLM | Cloud LLM | Hybrid |
|---|---|---|---|---|---|
| Accuracy (structured) | 20% | 5 | 4 | 5 | 5 |
| Accuracy (skills) | 15% | 3 | 4 | 5 | 4 |
| Accuracy (experience) | 20% | 2 | 4 | 5 | 4 |
| Accuracy (projects) | 10% | 2 | 3 | 4 | 3 |
| Latency | 10% | 5 | 2 | 3 | 3 |
| Cost | 10% | 5 | 5 | 3 | 4 |
| Privacy | 5% | 5 | 5 | 2 | 5 |
| Infrastructure burden | 5% | 5 | 2 | 5 | 2 |
| **Weighted Total** | 100% | **3.85** | **3.65** | **4.10** | **3.95** |

## Recommendation: **Hybrid (Rules + Local LLM)** — *with Cloud LLM as fallback*

### Why Hybrid Wins
1. **Best accuracy/cost/privacy balance** — Rules handle 80% of structured fields perfectly; LLM only for fuzzy extraction (years, metrics, project descriptions)
2. **Job-agent schema alignment** — Local LLM can be prompted to output exact `resume_inventory.json` schema
3. **Progressive enhancement** — Ship rule-based v1 immediately; add LLM when GPU available
4. **Fallback to cloud** — If local LLM unavailable, OpenRouter GPT-4o-mini costs ~$0.001/resume

## Implementation Phases

### Phase 1: Rule-Based MVP (Week 1-2)
- `pdfplumber` + `python-docx` for text extraction
- Regex/heuristics for: name, email, phone, LinkedIn, GitHub, location
- Skills: keyword matching against known tech stack list
- Experience: section detection → bullet extraction → keyword tagging
- Output: partial `resume_inventory.json` → map to Profile fields

### Phase 2: Local LLM Enhancement (Week 3-4)
- Ollama + Llama 3.1 8B (or Nemotron-3-Ultra if available)
- Prompt: "Extract resume into this JSON schema: [job-agent schema]"
- Focus on: years per skill, quantified metrics, project descriptions
- Merge with Phase 1 results (LLM wins on conflicts)

### Phase 3: GitHub/LinkedIn Enrichment (Week 5-6)
- **GitHub**: `GET /users/{username}/repos` + languages API → skills, projects, portfolio_urls
- **LinkedIn**: If user provides profile URL → scrape public sections (name, headline, location, skills) — *best effort, fragile*
- Merge: GitHub skills → `target_tags`; repo topics → `custom_answers`; languages → skills

### Phase 4: Onboarding Wizard + Re-sync (Week 7-8)
- Multi-step wizard: Upload → Parse → Review/Edit → Save
- "Re-sync from GitHub" button on Profile page
- Conflict resolution UI: "We found X from GitHub, you have Y — keep which?"

## Field Mapping: resume_inventory.json → JobBorg Profile

| JobBorg Profile Field | Source in resume_inventory.json | Parsing Priority |
|---|---|---|
| `target_titles` | `lane_profiles.*.title`, `roles.*.title` | Rule-based |
| `target_tags` | `skills.*.items` + GitHub languages/topics | Rule + LLM |
| `target_locations` | `contact.location`, `roles.*.location` | Rule-based |
| `custom_answers` (skills) | `roles.*.bullets.skills` + years inference | LLM |
| `custom_answers` (experience) | `roles.*.bullets.text` + metrics | LLM |
| `custom_answers` (projects) | `projects.*.bullets`, GitHub repos | LLM + API |
| `custom_answers` (education) | `education.*` | Rule-based |
| `portfolio_url` | `contact.github`, `projects.*.url` | Rule-based |
| `headline` | `lane_profiles.*.headline` | Rule-based |
| `full_name` | `contact.name` | Rule-based |

## Experiment to Validate (Run This Week)

```bash
# 1. Collect 5 diverse test resumes (PDF/DOCX)
# 2. Run each through 3 parsers:
#    a) rules_only.py
#    b) local_llama.py (if GPU) or openrouter.py
#    c) hybrid.py
# 3. Score each output against ground truth (manual annotation)
# 4. Measure: field coverage %, accuracy %, latency, cost
```

**Ground truth fields to score**: name, email, phone, location, 20 skills, 5 roles with years, 3 projects, education.

---

# Next Steps

1. **Run experiment** this week → validate matrix scores
2. **If Hybrid confirmed** → implement Phase 1 immediately (rule-based, no GPU needed)
3. **Integrate with unified profile plan** — parsing populates Tab 1 (Profile) + Tab 2 (Auto-apply Answers)
4. **Feed parsed data into learning loop** — parsed custom_answers become high-confidence seeds