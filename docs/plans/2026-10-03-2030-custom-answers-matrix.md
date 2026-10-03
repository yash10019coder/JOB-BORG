---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T20:30:00Z"
scope: "Decision matrix: Custom Answers (replacing ExplicitAnswers.other)"
---

# Decision Matrix: Custom Answers Model

## Current State
- **ExplicitAnswers.OTHER**: Single free-text field, no structure
- **AutoApplyDraft.form_schema_snapshot**: Contains custom questions from applications (300+ location, 30+ experience, 25+ other)

## Options

| Option | Model |
|---|---|
| **A: Structured Q&A Pairs** | `custom_answers: [{question, answer, category, confidence}]` — user manages list |
| **B: Category-Keyed Dict** | `custom_answers: {"experience": {"python": "5 years", "k8s": "3 years"}, "relocation": "Berlin"}` |
| **C: Free-Text "Other" (Keep)** | Single text field, LLM parses at apply time |
| **D: Learned-Only (No User UI)** | System learns from applications, user never edits directly |

---

## Real Custom Questions (from AutoApplyDraft analysis)

| Category | Count | Example Questions |
|---|---|---|
| Experience/Years | 30+ | "Years of Python experience", "Years with Kubernetes", "Total years of experience" |
| Specific Technologies | 25+ | "Experience with React", "AWS certification level", "GCP services used" |
| Relocation | 20+ | "Willing to relocate to Berlin?", "Open to hybrid in London?" |
| Projects/Portfolio | 15+ | "Link to GitHub project", "Describe a project you're proud of" |
| Education | 10+ | "Highest degree", "University name", "Graduation year" |
| Availability | 8+ | "Notice period", "Available start date", "Overtime willingness" |
| Security/Compliance | 8+ | "Security clearance", "Background check consent", "Non-compete" |
| Visa/Immigration Details | 12+ | "Visa expiry date", "Current visa type", "OPT/CPT status" |

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Covers real question diversity | 30% |
| LLM matchability (at apply time) | 25% |
| User manageability | 20% |
| Learning loop write target | 15% |
| Implementation simplicity | 10% |

---

## Scoring

| Criterion | Weight | A: Structured Q&A | B: Category Dict | C: Free-Text | D: Learned-Only |
|---|---|---|---|---|---|
| Covers diversity | 30% | 5 | 4 | 2 | 3 |
| LLM matchability | 25% | 5 | 4 | 3 | 4 |
| User manageability | 20% | 3 | 4 | 5 | 5 |
| Learning loop target | 15% | 5 | 5 | 2 | 4 |
| Implementation | 10% | 3 | 4 | 5 | 4 |
| **Weighted Total** | 100% | **4.55** | **4.15** | **3.15** | **3.85** |

---

## Recommendation: **Option A (Structured Q&A Pairs)** with Category Grouping

### Schema

```python
# Profile.custom_answers = [
#   {"question": "Years of Python experience", "answer": "5 years", "category": "experience", "confidence": 0.9},
#   {"question": "Willing to relocate to Berlin", "answer": "Yes, hybrid preferred", "category": "relocation", "confidence": 0.8},
#   {"question": "Security clearance level", "answer": "Secret", "category": "compliance", "confidence": 1.0},
# ]

# Categories (fixed, for UI grouping)
CUSTOM_ANSWER_CATEGORIES = [
    "experience",       # Years per tech, total experience
    "technologies",     # Specific tech proficiency
    "relocation",       # Willingness, preferred cities
    "projects",         # Portfolio links, project descriptions
    "education",        # Degree, university, graduation
    "availability",     # Notice period, start date
    "compliance",       # Security clearance, background check
    "visa_details",     # Visa type, expiry, OPT/CPT
    "other",            # Catch-all
]
```

### LLM Matching at Apply Time

```python
def match_custom_answer(app_question, custom_answers):
    """Semantic match application question to user's custom answers."""
    # Embed app_question + each custom_answer.question
    # Return best match above threshold (0.75)
    # If match: return custom_answer.answer
    # Else: return "needs_review"
```

### Learning Loop Integration

| Signal | Creates/Updates |
|---|---|
| Review queue: human fills "Years of Python" | `custom_answers` +{question: "Years of Python experience", answer: "5 years", category: "experience"} |
| Resume parsing: extracts "5 years Python" | Suggests custom_answer (user accepts) |
| Application outcome: UNANSWERABLE_REQUIRED for "Kubernetes experience" | Suggests new custom_answer (gap detection) |

### UI (Tab 2: Auto-apply Answers → Custom Answers Section)

```
Custom Answers
┌─────────────────────────────────────────────────────────────┐
│ Experience (3)                          [+ Add]             │
│   Years of Python experience        → "5 years"      [✓]   │
│   Years of Kubernetes experience    → "3 years"      [✓]   │
│   Total years of experience         → "6 years"      [✓]   │
├─────────────────────────────────────────────────────────────┤
│ Relocation (1)                                                   │
│   Willing to relocate to Berlin   → "Yes, hybrid"     [✓]   │
├─────────────────────────────────────────────────────────────┤
│ Compliance (1)                                                 │
│   Security clearance level        → "Secret"          [✓]   │
└─────────────────────────────────────────────────────────────┘
[Add Custom Answer] → Modal: Question | Answer | Category
```

### Auto-Apply Resolution Order (Updated)

```python
def get_answer(user, category, app_question=None):
    # 1. Explicit override
    if override := ExplicitAnswer.objects.filter(user=user, category=category).first():
        return override.answer_text, "override"
    
    # 2. Structured Profile fields
    if category == "work_authorization":
        return derive_work_auth(user.profile), "profile"
    if category == "sponsorship":
        return derive_sponsorship(user.profile), "profile"
    if category == "salary_expectation":
        return derive_salary_band(user.profile), "profile"
    
    # 3. Custom answers (semantic match)
    if app_question and user.profile.custom_answers:
        match = match_custom_answer(app_question, user.profile.custom_answers)
        if match:
            return match.answer, "custom_answer"
    
    # 4. Fallback to ExplicitAnswers.other (legacy)
    if category == "other":
        return user.profile.custom_answers.get("other", ""), "profile"
    
    return "", "none"
```

---

## Migration from ExplicitAnswers.OTHER

```python
# One-time migration
for ea in ExplicitAnswer.objects.filter(category="other"):
    if ea.answer_text:
        Profile.objects.filter(user=ea.user).update(
            custom_answers=JSONFieldAppend(
                {"question": "Other", "answer": ea.answer_text, "category": "other"}
            )
        )
```

---

## Next: Questions Panel Decision Matrix