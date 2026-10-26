---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
created_at: "2026-10-03T20:20:00Z"
scope: "Decision matrix: Salary model (region dropdowns vs global min_salary)"
---

# Decision Matrix: Salary Model

## Current State
- **Profile**: `min_salary` (integer, implied USD) + `salary_region_*` (7 dropdowns: US, CA, UK, AU, IN, SG, EU)
- **ExplicitAnswers**: `salary_expectation` (band: "<50k", "50-75k", ..., "300-400k")
- **Auto-apply**: Needs salary for specific job's country/region

## Options

| Option | Model |
|---|---|
| **A: Region Dropdowns Only** | 7 `salary_region_*` dropdowns are primary. No global `min_salary`. Job's region → exact dropdown. |
| **B: Global min_salary + Region Overrides** | `min_salary` (USD) is fallback. Region dropdowns override for specific regions. Resolution: region → global. |
| **C: Global min_salary Only** | Single `min_salary` (with currency selector). Convert to job's currency at apply time. |
| **D: Region Dropdowns + Global Fallback (Current Hybrid)** | Region dropdowns primary, `min_salary` as global fallback for regions not covered. |

## Real Application Questions (from analysis)

| Pattern | Count | Example |
|---|---|---|
| "What are your salary expectations?" | 26 | Open text / band |
| "What range does your salary expectation fall into?" | 5 | Dropdown with bands |
| "Desired salary" / "Minimum salary required" | 8 | Number input |
| "Please select your desired salary range" | 6 | Band selector |

**Key insight**: Most ask for a **single number or range**. Region-specific is rare in application forms (employer posts one range per job).

---

## Evaluation Criteria

| Criterion | Weight |
|---|---|
| Matches application form UX | 30% |
| User effort (cognitive load) | 25% |
| Covers global job search | 20% |
| Implementation simplicity | 15% |
| Learning loop compatibility | 10% |

---

## Scoring

| Criterion | Weight | A: Regions Only | B: Global + Override | C: Global Only | D: Hybrid |
|---|---|---|---|---|---|
| Matches app UX | 30% | 3 | 4 | 5 | 4 |
| User effort | 25% | 2 | 3 | 5 | 3 |
| Global coverage | 20% | 3 | 5 | 4 | 5 |
| Implementation | 15% | 4 | 3 | 5 | 3 |
| Learning loop | 10% | 4 | 4 | 3 | 4 |
| **Weighted Total** | 100% | **3.25** | **3.85** | **4.50** | **3.75** |

---

## Recommendation: **Option B (Global min_salary + Region Overrides)** — with currency selector

### Why Not C (Global Only)?
- Highest score but: 7 regions already implemented, users may want different expectations per region
- USD-only fails for IN (lakhs), SG (monthly), EU (annual EUR)

### Why Not A (Regions Only)?
- Too many fields (7 dropdowns) for new users
- No fallback for uncovered regions

### Why B Wins:
- **Single global field** for onboarding (simple)
- **Region overrides** for power users (US: 175-200k, IN: 30-40L)
- **Currency selector** on global field (USD, INR, EUR, SGD, CAD, AUD, GBP)
- Learning loop can populate both

### Implementation

```python
# Profile fields
min_salary = IntegerField(null=True)  # in selected currency
salary_currency = CharField(choices=CURRENCY_CHOICES, default="USD")
salary_region_US = CharField(choices=US_BANDS, blank=True)
salary_region_CA = CharField(choices=CA_BANDS, blank=True)
salary_region_UK = CharField(choices=UK_BANDS, blank=True)
salary_region_AU = CharField(choices=AU_BANDS, blank=True)
salary_region_IN = CharField(choices=IN_BANDS, blank=True)
salary_region_SG = CharField(choices=SG_BANDS, blank=True)
salary_region_EU = CharField(choices=EU_BANDS, blank=True)

# Resolution for auto-apply
def get_salary_for_job(profile, job_country):
    region = region_for_country(job_country)  # US, CA, UK, AU, IN, SG, EU
    region_field = f"salary_region_{region}"
    if getattr(profile, region_field):
        return parse_band(getattr(profile, region_field), region)
    elif profile.min_salary:
        return convert_currency(profile.min_salary, profile.salary_currency, job_currency)
    return None
```

### UI

```
Tab 1: Profile
  Minimum Salary: [120000] [USD ▼]  (global fallback)

Tab 2: Auto-apply Answers
  Salary by Region:
  🇺🇸 United States: [175-200k ▼]
  🇨🇦 Canada:        [150-180k ▼]  
  🇬🇧 United Kingdom: [__________]
  🇦🇺 Australia:      [__________]
  🇮🇳 India:          [30-40L ▼]
  🇸🇬 Singapore:      [__________]
  🇪🇺 EU:             [__________]
    Help: "Region-specific overrides. Global fallback used if empty."
```

### Band Definitions by Region

| Region | Currency | Bands |
|---|---|---|
| US | USD | <50k, 50-75k, 75-100k, 100-125k, 125-150k, 150-175k, 175-200k, 200-250k, 250-300k, 300-400k, 400k+ |
| CA | CAD | <60k, 60-90k, 90-120k, 120-150k, 150-180k, 180-220k, 220-280k, 280k+ |
| UK | GBP | <35k, 35-50k, 50-65k, 65-80k, 80-100k, 100-130k, 130-160k, 160k+ |
| AU | AUD | <70k, 70-100k, 100-130k, 130-160k, 160-200k, 200-250k, 250k+ |
| IN | INR (lakhs) | <10L, 10-15L, 15-20L, 20-30L, 30-40L, 40-50L, 50-75L, 75L+ |
| SG | SGD | <60k, 60-90k, 90-120k, 120-150k, 150-180k, 180-220k, 220k+ |
| EU | EUR | <40k, 40-55k, 55-70k, 70-90k, 90-110k, 110-140k, 140k+ |

---

## Next: "Other" Field / Custom Answers Decision Matrix