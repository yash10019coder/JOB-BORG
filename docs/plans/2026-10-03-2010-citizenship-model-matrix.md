---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T20:10:00Z"
scope: "Decision matrix: Citizenship field mental model"
---

# Decision Matrix: Citizenship Mental Model

## Options

| Option | Mental Model | Use Case |
|---|---|---|
| **A: Passports Held** | "Countries whose passports you hold" | Answers "Are you a citizen of X?" questions |
| **B: Work Rights Without Visa** | "Countries you can work in without sponsorship" | Equivalent to visa-free work authorization |
| **C: Both (Dual Model)** | Separate fields: `citizenship_countries` (passports) + `visa_free_countries` (work rights) | Maximum precision |
| **D: Single Field "Work Authorization Countries"** | Merge citizenship + visa-free into one: "Countries where you can work without employer sponsorship" | Simplest for user |

---

## Real Application Questions (from AutoApplyDraft analysis)

| Question Pattern | Count | What It Actually Asks |
|---|---|---|
| "Are you a US citizen?" / "To be considered, you must be a US Citizen" | 12 | Citizenship (Option A) |
| "Are you authorized to work in the US without sponsorship?" | 37 | Visa-free work right (Option B) |
| "Are you legally authorized to work in [country]?" | 65 | Work authorization (visa status) |
| "Will you now or in the future require visa sponsorship?" | 73 | Sponsorship need (inverse of B) |

**Key insight**: Citizenship questions (A) are a **subset** of work authorization questions. Most applications ask about work authorization, not citizenship directly.

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Covers real application questions | 30% |
| User clarity (mental model) | 25% |
| Maps to auto-apply answers | 20% |
| Data accuracy (user knows truth) | 15% |
| Implementation simplicity | 10% |

---

## Scoring

| Criterion | Weight | A: Passports | B: Visa-Free | C: Dual Model | D: Single Auth |
|---|---|---|---|---|---|
| Covers real questions | 30% | 3 | 4 | 5 | 4 |
| User clarity | 25% | 5 | 3 | 2 | 4 |
| Maps to auto-apply | 20% | 3 | 5 | 5 | 4 |
| Data accuracy | 15% | 5 | 3 | 4 | 2 |
| Implementation simplicity | 10% | 4 | 3 | 2 | 5 |
| **Weighted Total** | 100% | **3.85** | **3.65** | **3.90** | **3.85** |

---

## Recommendation: **Option A (Passports Held) + Derived Visa-Free**

### Why Not B/D?
- Users know their passports with certainty
- Visa-free work rights are complex (depends on visa type, duration, employer)
- "Work authorization countries" conflates two different things

### Why Not C (Dual)?
- Adds UI complexity for marginal gain
- Visa-free can be **derived** from visa_status_by_country

### Implementation

```python
# Profile model
citizenship_countries = JSONField(default=list)  # ["USA", "CAN", "IND"] - passports held

# Derived for auto-apply
def get_visa_free_countries(profile):
    """Countries where user has work authorization WITHOUT sponsorship."""
    visa_free = []
    for country, status in profile.visa_status_by_country.items():
        if status in ["citizen", "permanent_resident", "no_sponsorship_needed"]:
            visa_free.append(country)
    return visa_free

# Auto-apply mapping
def answer_citizenship_question(question, profile):
    if "citizen" in question.lower():
        country = extract_country(question)
        return "yes" if country in profile.citizenship_countries else "no"
    elif "authorized to work" in question.lower() and "sponsor" not in question.lower():
        country = extract_country(question)
        return "yes" if country in get_visa_free_countries(profile) else "no"
    elif "sponsor" in question.lower():
        country = extract_country(question)
        status = profile.visa_status_by_country.get(country)
        return "yes" if status == "requires_sponsorship" else "no"
```

### UI

```
Citizenship (Passports Held)
[✓] United States (USA)
[✓] Canada (CAN)
[  ] India (IND)
[  ] Germany (DEU)
    Help: "Select countries whose passports you hold. 
    Used for 'Are you a citizen of X?' questions."

Work Authorization by Country
United States (USA) → [Citizen ▼]  (derived: visa-free)
Germany (DEU)       → [Requires Sponsorship ▼]
    Help: "Your work authorization status per country.
    Auto-apply uses this for sponsorship questions."
```

---

## Migration

- Existing `citizenship_countries` (if any) → passports held
- Existing `visa_status_by_country` values like "citizen" → both citizenship + visa-free
- New users: empty citizenship, add passports explicitly