---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T15:30:00Z"
scope: "Self-learning profile loop that dynamically improves Profile from explicit answers, application questions, outcomes, and match signals"
---

# Goal Capsule

**Objective**: Build a continuous learning loop where the user's Profile automatically improves from every interaction — explicit answers given, application questions encountered, application outcomes (success/failure), and match quality signals — reducing manual maintenance while increasing auto-apply success rate.

**Active Area**: Profile model, AutoApplyDraft analysis, learning engine (Celery tasks), suggestion UI (Learning tab + inline hints + banner + email), ExplicitAnswers integration. Surrounding areas (matching, ingestion, classification) are consumers of improved Profile data.

**Success Criteria**:
1. `unanswerable_required` exclusion rate drops ≥30% within 30 days of activation
2. Draft-to-Applied conversion rate increases ≥15%
3. User accepts ≥60% of suggested updates
4. Zero unwanted profile changes (high-risk fields always require approval)

---

# Product Contract

## 1. Learning Signals (Inputs)

| Signal | Source | Frequency | What It Captures |
|---|---|---|---|
| **Explicit answers** | `Profile` form saves, `ExplicitAnswer` overrides, `custom_answers` edits | On save | User-declared preferences, structured Q&A |
| **Application questions** | `AutoApplyDraft.form_schema_snapshot.fields` | Per draft created | What employers actually ask (salary, visa, experience, custom) |
| **Application outcomes** | `AutoApplyDraft.status`, `reason_code`, `error_message` | Per status change | Success (Applied), gaps (UNANSWERABLE_REQUIRED), failures |
| **Match quality** | `UserJobMatch.score`, `JobApplication.status` (saved/applied/dismissed) | Per recommendation | Which matched jobs user acts on vs ignores |
| **Review queue edits** | Human-filled answers in review queue before send | Per draft sent | Ground-truth answers for custom questions |

## 2. Learning Engine

### Hybrid Approach: Rules + Embeddings

#### Rule-Based (Structured Fields — High Confidence)
```python
# Examples of deterministic patterns
- Salary: If 3+ drafts for US jobs have salary_expectation="175-200k" → suggest salary_region_US="175-200k"
- Visa: If 3+ drafts for DE jobs ask sponsorship → suggest visa_status_by_country.DEU="requires_sponsorship"
- Citizenship: If 3+ drafts ask "Are you a US citizen?" and user answers yes → suggest citizenship_countries add USA
- Locations: If 3+ drafts for remote jobs in CA → suggest target_locations add "Canada"
```

#### Embedding-Based (Custom Questions — Semantic)
1. **Extract** all custom (non-standard) questions from `form_schema_snapshot` across user's drafts
2. **Embed** question texts using sentence-transformers (stored in `pgvector`)
3. **Cluster** similar questions (k-means or HDBSCAN)
4. **Find consensus** answers per cluster from review-queue edits + explicit answers
5. **Generate** custom_answers suggestions: `{"question": "Years of Python experience", "answer": "5 years", "confidence": 0.92, "supporting_drafts": [123, 456, 789]}`

### Risk Classification
| Field / Update Type | Risk | Action |
|---|---|---|
| New `custom_answers` entry | Low | Auto-add (user can delete) |
| Existing `custom_answers` answer change | Medium | Suggest if confidence ≥0.8 |
| `salary_region_*` change | High | Suggest (require approval) |
| `visa_status_by_country` change | High | Suggest (require approval) |
| `citizenship_countries` add/remove | High | Suggest (require approval) |
| `target_locations` change | High | Suggest (require approval) |
| `target_titles`/`target_tags` change | Medium | Suggest if confidence ≥0.85 |

### Confidence Scoring
- **Count**: Number of consistent observations (3+ = high)
- **Consistency**: % of observations with same answer (100% = high)
- **Recency**: Weight recent drafts higher (exponential decay, half-life 30 days)
- **Outcome weight**: Applied drafts count 2×; Failed/Excluded count 1× for gap detection
- **Review-queue weight**: Human-provided answers count 3×

## 3. Execution Contexts (All Three)

### A. Incremental Hook (Post-Application)
```python
# apps/auto_apply/tasks.py:submit_auto_apply_draft
# On status change to APPLIED | FAILED | EXCLUDED:
trigger_incremental_learning(user_id, draft_id)
```
- Processes single draft's questions/answers/outcome
- Updates embeddings index
- Checks for new high-confidence patterns
- Emits suggestions if threshold crossed

### B. Daily Batch (Celery Beat)
```python
# config/settings/base.py:CELERY_BEAT_SCHEDULE
"daily-profile-learning": {
    "task": "apps.accounts.tasks.daily_profile_learning",
    "schedule": crontab(hour=3, minute=0),  # 3 AM
}
```
- Full re-analysis of all user's drafts (last 90 days)
- Re-clusters embeddings
- Recomputes all confidence scores
- Generates fresh suggestion batch
- Sends weekly digest email (Mondays)

### C. On-Demand (User-Initiated)
```python
# POST /profile/learn/  (new endpoint)
# Button: "Improve my profile" on Learning tab
```
- Runs incremental + batch for this user immediately
- Returns suggestion count, redirects to Learning tab

## 4. Suggestion Model & Storage

```python
# apps/accounts/models.py addition
class ProfileSuggestion(models.Model):
    class FieldType(models.TextChoices):
        CUSTOM_ANSWER = "custom_answer", "Custom Answer"
        SALARY_REGION = "salary_region", "Salary by Region"
        VISA_STATUS = "visa_status", "Visa Status"
        CITIZENSHIP = "citizenship", "Citizenship"
        TARGET_LOCATIONS = "target_locations", "Target Locations"
        TARGET_TITLES = "target_titles", "Target Titles"
        TARGET_TAGS = "target_tags", "Target Tags"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        AUTO_APPLIED = "auto_applied", "Auto-Applied"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_suggestions")
    field_type = models.CharField(max_length=32, choices=FieldType.choices)
    field_key = models.CharField(max_length=64)  # e.g., "US", "DEU", "Python experience"
    suggested_value = models.JSONField()  # value to set
    current_value = models.JSONField(null=True, blank=True)  # current profile value
    confidence = models.FloatField()  # 0.0–1.0
    evidence = models.JSONField(default=dict)  # {"draft_ids": [...], "pattern": "..."}
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="resolved_suggestions")

    class Meta:
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "field_type"]),
        ]
```

## 5. User-Facing UI

### A. Learning Tab (in Unified Profile)
```
/profile/ → Tab 3: "Learning"
Sections:
1. Pending Suggestions (accept/reject each)
2. Applied Updates (history with undo for 30 days)
3. Learning Dashboard: charts — suggestions/month, acceptance rate, unanswerable_required trend
```

### B. Inline Field Hints
On Profile + Auto-apply Answers tabs:
```html
<!-- Next to salary_region_US dropdown -->
<span class="learned-hint" title="Learned from 4 applications (last: 2 days ago)">
  💡 Learned: "175-200k" from 4 US applications
  <button class="accept-hint">Accept</button>
</span>
```

### C. Profile Page Banner
```html
<div class="learning-banner" id="learning-banner" style="display: none;">
  We have <strong>{{ pending_count }}</strong> suggested updates from recent applications.
  <a href="{% url 'profile' %}#learning">Review</a> | <button id="dismiss-banner">Dismiss</button>
</div>
```

### D. Weekly Digest Email
- Sent Mondays 9 AM (Celery Beat)
- Template: `email/profile_learning_digest.html`
- Content: "This week: 3 custom answers added, US salary updated, 1 visa gap detected"
- CTA: "Review suggestions" → Learning tab
- Unsubscribe link in footer

## 6. Negative Signal Learning

### Failed Applications → Gap Detection
- On `UNANSWERABLE_REQUIRED`: extract question → if no matching `custom_answer` → suggest new entry
- On `SCHEMA_MISMATCH` / `FORM_LOAD_FAILED`: log for debugging, don't learn
- On `CAPTCHA_CHALLENGED` / `VERIFICATION_*`: not profile-related, ignore

### Dismissed Recommendations → Preference Learning
- Track `UserJobMatch` → `JobApplication.DISMISSED`
- If >5 dismissals for same `target_location`/`target_title`/`target_tag` → suggest removal/deprioritization
- Surface in Learning tab: "You've dismissed 8 matches in 'San Francisco' — remove from target locations?"

## 7. Integration with Existing Systems

### Auto-Apply Drafting (`apps/auto_apply/services/drafting.py`)
- **Before**: Uses Profile + ExplicitAnswers + custom_answers
- **After**: Also uses high-confidence pending suggestions (auto-applied ones) as fallbacks
- Custom answers matched by embedding similarity to application question

### Matching (`apps/matching/services.py`)
- Improved `target_titles`/`target_tags`/`target_locations` → better prefilter → better scores
- No code change needed; consumes improved Profile

### ExplicitAnswers (Unified Tab 2)
- Overrides still win over learned values
- Learning suggestions appear as "Auto-apply will use: [learned] ▼ [override]"
- Accepting a suggestion updates the derived value, not the override

## 8. Data Flow Summary

```
User applies to job
       │
       ▼
AutoApplyDraft created (form_schema_snapshot captured)
       │
       ▼
Draft sent → status changes (APPLIED/FAILED/EXCLUDED)
       │
       ├──▶ Incremental learning hook (immediate)
       │
       ▼
Daily batch (3 AM): re-analyze all user drafts
       │
       ├──▶ Rule engine: structured field patterns
       │
       ├──▶ Embedding engine: custom question clusters
       │
       ▼
ProfileSuggestion rows created (PENDING)
       │
       ├──▶ Low-risk (custom_answers, high-confidence) → AUTO_APPLIED
       │
       └──▶ High-risk → UI surfaces (banner, tab, inline hints, email)
       │
       ▼
User accepts/rejects → Profile updated → better drafts next cycle
```

## 9. Migration & Backward Compatibility

- **New model**: `ProfileSuggestion` (migration)
- **New field**: `Profile.custom_answers` (JSON, default=[]) — already in unified profile plan
- **Embeddings table**: `apps.accounts.models.QuestionEmbedding` (pgvector, new migration)
- **Existing data**: Backfill by running daily batch on all users once after deploy
- **ExplicitAnswer.OTHER**: Migrated to `custom_answers` as before

## 10. Privacy & Control

- **User owns data**: All suggestions visible, editable, deletable
- **Opt-out**: Profile field `learning_enabled` (default=True) — disables all learning
- **Data retention**: Suggestions auto-delete after 90 days if unresolved
- **Export**: "Download my learning data" button on Learning tab (GDPR)

---

# How This Work Fits Together

| Area | Relationship |
|---|---|
| Unified Profile (previous plan) | Provides structured fields (visa, citizenship, salary, custom_answers) that learning updates |
| Auto-apply drafting | Consumes improved Profile; produces learning signals (questions, outcomes) |
| Matching | Consumes improved Profile (better targets → better matches) |
| Review queue | Human answers feed custom_answers learning (high-weight signal) |
| Celery/Beat | Runs incremental + batch learning tasks |

---

# Open Questions for Planning

1. **Embedding model choice** — `sentence-transformers/all-MiniLM-L6-v2` (384-dim, fast) vs `all-mpnet-base-v2` (768-dim, better quality)?
2. **Clustering algorithm** — HDBSCAN (no k needed) vs k-means (need k estimate per user)?
3. **Confidence thresholds** — Start conservative (0.85 for auto-apply, 0.7 for suggest), tune from data?
4. **Batch size** — Daily batch processes all users; parallelize per-user or chunk?
5. **Email template** — Reuse existing email infrastructure or new template?
6. **Learning tab permissions** — Staff/admin can see for debugging? (No — user-only)