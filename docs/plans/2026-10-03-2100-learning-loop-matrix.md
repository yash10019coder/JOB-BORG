---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T21:00:00Z"
scope: "Decision matrix: Self-Learning Profile Loop"
---

# Decision Matrix: Self-Learning Profile Loop

## Current State
- No learning loop exists
- Profile is static until user manually edits
- Auto-apply failures (UNANSWERABLE_REQUIRED) don't improve Profile

## Options

| Option | Learning Mechanism |
|---|---|
| **A: Rule-Based Only** | SQL patterns: "3+ consistent answers → suggest/update" |
| **B: Embeddings + Clustering** | pgvector: embed questions, cluster, find consensus |
| **C: LLM-Based Extraction** | Periodic LLM: "Here are all Q/A/outcomes → what Profile updates?" |
| **D: Hybrid (Rules + Embeddings)** | Rules for structured (salary, visa), embeddings for custom questions |

---

## Learning Signals Available

| Signal | Volume | Quality | Latency |
|---|---|---|---|
| Explicit user edits (Profile save) | Low | High (intentional) | Immediate |
| ExplicitAnswers overrides | Low | High | Immediate |
| Custom answers (review queue) | Medium | High (human-verified) | Per draft |
| Application questions (form_schema) | High | Medium (raw) | Per draft |
| Application outcomes (Applied/Failed) | High | Medium (noisy) | Per draft |
| Match quality (saved/applied/dismissed) | High | Low (implicit) | Continuous |

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Reduces unanswerable_required | 25% |
| Increases draft→applied rate | 20% |
| User trust (no unwanted changes) | 20% |
| Implementation complexity | 15% |
| Explainability (why this suggestion?) | 10% |
| Maintenance burden | 10% |

---

## Scoring

| Criterion | Weight | A: Rules Only | B: Embeddings | C: LLM | D: Hybrid |
|---|---|---|---|---|---|
| Reduces unanswerable | 25% | 4 | 4 | 5 | 5 |
| Increases applied rate | 20% | 3 | 4 | 5 | 4 |
| User trust | 20% | 5 | 3 | 2 | 4 |
| Implementation | 15% | 5 | 2 | 3 | 3 |
| Explainability | 10% | 5 | 3 | 2 | 4 |
| Maintenance | 10% | 5 | 2 | 2 | 3 |
| **Weighted Total** | 100% | **4.25** | **3.25** | **3.60** | **3.95** |

---

## Recommendation: **Option D (Hybrid: Rules + Embeddings)** with **High-Confidence Auto-Apply**

### Why Not A (Rules Only)?
- Can't handle semantic variation in custom questions
- "Years of Python experience" vs "Python years of experience" → different clusters needed

### Why Not B (Embeddings Only)?
- Overkill for structured fields (salary, visa)
- Harder to explain ("why did you suggest this?")
- Cold start: needs data before clustering works

### Why Not C (LLM)?
- Cost per run
- Black box, hard to debug
- Hallucination risk on sparse data

### Why D Wins:
- **Rules**: deterministic, explainable, instant for salary/visa/citizenship
- **Embeddings**: handles semantic custom questions, improves with data
- **Auto-apply gate**: only high-confidence (0.85+) writes directly; others suggest

---

## Hybrid Architecture

### Rule Engine (Structured Fields)

```python
RULES = [
    # Salary
    {
        "trigger": "salary_expectation",
        "condition": "3+ drafts for same region with same band",
        "action": "suggest salary_region_{region} = band",
        "confidence": 0.9,
        "auto_apply": True,
    },
    # Visa
    {
        "trigger": "sponsorship_question",
        "condition": "3+ drafts for same country asking sponsorship",
        "action": "suggest visa_status_by_country.{country} = requires_sponsorship",
        "confidence": 0.85,
        "auto_apply": True,
    },
    # Citizenship
    {
        "trigger": "citizenship_question",
        "condition": "3+ drafts asking 'Are you a {country} citizen?' answered yes",
        "action": "suggest citizenship_countries add {country}",
        "confidence": 0.9,
        "auto_apply": True,
    },
    # Locations
    {
        "trigger": "location_question",
        "condition": "3+ drafts for remote jobs in {country}",
        "action": "suggest target_locations add {country}",
        "confidence": 0.7,
        "auto_apply": False,  # Suggest only
    },
]
```

### Embedding Engine (Custom Questions)

```python
# 1. Extract custom questions from form_schema_snapshot
# 2. Embed with sentence-transformers (all-MiniLM-L6-v2, 384-dim)
# 3. Store in pgvector: QuestionEmbedding(user_id, question_text, embedding, draft_id, answer)
# 4. Cluster per user (HDBSCAN, min_cluster_size=3)
# 5. Per cluster: find consensus answer from review_queue + explicit answers
# 6. Generate suggestion:
#    {
#      "question": "Years of Python experience",
#      "answer": "5 years",
#      "category": "experience",
#      "confidence": 0.92,
#      "supporting_drafts": [123, 456, 789],
#      "cluster_id": 7
#    }
```

### Confidence Scoring

| Factor | Weight | Formula |
|---|---|---|
| Count | 30% | min(count / 5, 1.0) |
| Consistency | 25% | % same answer across observations |
| Recency | 20% | exp(-days_since_last / 30) |
| Outcome | 15% | Applied=2x, Failed/Excluded=1x (for gaps) |
| Review-queue | 10% | Human answer = 3x weight |

**Auto-apply threshold**: 0.85  
**Suggest threshold**: 0.70

---

## Execution Contexts (All Three)

### 1. Incremental Hook (Per Draft Outcome)
```python
# In submit_auto_apply_draft task, after status change:
if draft.status in [APPLIED, FAILED, EXCLUDED]:
    trigger_incremental_learning(user_id, draft_id)
```
- Processes single draft's Q/A/outcome
- Updates embedding index
- Checks rule triggers for this draft's questions
- Emits ProfileSuggestion if threshold crossed

### 2. Daily Batch (Celery Beat, 3 AM)
```python
# Full re-analysis of last 90 days
# Re-clusters embeddings
# Recomputes all confidence scores
# Generates fresh suggestion batch
# Sends weekly digest email (Mondays)
```

### 3. On-Demand (User-Initiated)
```
POST /profile/learn/  → "Improve my profile" button
```
- Runs incremental + batch for this user
- Returns suggestion count, redirects to Learning tab

---

## ProfileSuggestion Model

```python
class ProfileSuggestion(models.Model):
    FIELD_TYPES = [
        ("custom_answer", "Custom Answer"),
        ("salary_region", "Salary by Region"),
        ("visa_status", "Visa Status"),
        ("citizenship", "Citizenship"),
        ("target_locations", "Target Locations"),
        ("target_titles", "Target Titles"),
        ("target_tags", "Target Tags"),
    ]
    
    STATUS = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
        ("auto_applied", "Auto-Applied"),
    ]
    
    user = FK(User, related_name="profile_suggestions")
    field_type = CharField(choices=FIELD_TYPES)
    field_key = CharField(max_length=64)  # "US", "DEU", "Python experience"
    suggested_value = JSONField()
    current_value = JSONField(null=True)
    confidence = FloatField()  # 0.0-1.0
    evidence = JSONField()  # {"draft_ids": [...], "pattern": "..."}
    source = CharField(choices=[("rule", "Rule"), ("embedding", "Embedding")])
    status = CharField(choices=STATUS, default="pending")
    created_at = DateTimeField(auto_now_add=True)
    resolved_at = DateTimeField(null=True)
    resolved_by = FK(User, null=True, related_name="resolved_suggestions")
```

---

## User-Facing UI

### Learning Tab (Tab 3 in Unified Profile)
```
[Profile] [Auto-apply Answers] [Learning]

Pending Suggestions (3)
┌────────────────────────────────────────────────────────────┐
│ 💡 Custom Answer: "Years of Python experience" → "5 years" │
│    Based on 4 applications (last: 2 days ago) [Accept] [×] │
├────────────────────────────────────────────────────────────┤
│ 💡 Salary (US): "175-200k"                                  │
│    Based on 3 applications [Accept] [×]                    │
├────────────────────────────────────────────────────────────┤
│ 💡 Visa (DEU): "Requires Sponsorship"                      │
│    Based on 5 sponsorship questions [Accept] [×]           │
└────────────────────────────────────────────────────────────┘

Applied Updates (Last 30 days)
  ✓ Custom Answer: "Years of Kubernetes" → "3 years" (auto-applied)
  ✓ Salary (IN): "30-40L" (accepted)
  ✗ Visa (FRA): "Citizen" (rejected - user not French)

Learning Dashboard
  • Suggestions this month: 12 (8 auto-applied, 4 accepted)
  • Acceptance rate: 92%
  • Unanswerable_required rate: 18% → 12% (↓33%)
```

### Inline Field Hints (Tabs 1 & 2)
```
Salary (US): [175-200k ▼]  💡 Learned from 4 applications
                              [Accept] [Dismiss]
```

### Profile Banner
```
We have 3 suggested updates from recent applications.
[Review] [Dismiss]
```

### Weekly Digest Email (Mondays)
```
Subject: Your profile improved: 3 custom answers, US salary updated

This week:
• Added: "Years of Python experience" → "5 years"
• Updated: Salary (US) → "175-200k" 
• Detected gap: Visa sponsorship for Germany (5 questions)

[Review suggestions] → Learning tab
```

---

## Negative Signal Learning

### Failed Applications → Gap Detection
```python
if draft.reason_code == UNANSWERABLE_REQUIRED:
    question = extract_unanswered_question(draft)
    if not matches_custom_answer(question, user.profile.custom_answers):
        suggest_custom_answer(question, category=infer_category(question))
```

### Dismissed Recommendations → Preference Learning
```python
if JobApplication.objects.filter(user=user, status=DISMISSED).count() > 5:
    # Analyze dismissed jobs for common attributes
    if common_location in dismissed:
        suggest_remove_target_location(common_location)
    if common_title in dismissed:
        suggest_deprioritize_title(common_title)
```

---

## Privacy & Control

- **Opt-out**: `Profile.learning_enabled = False` disables all learning
- **Data retention**: Suggestions auto-delete after 90 days if unresolved
- **Export**: "Download my learning data" (GDPR)
- **Transparency**: Every suggestion shows evidence (draft IDs, pattern)

---

## Next: Resume Parsing Integration Decision Matrix