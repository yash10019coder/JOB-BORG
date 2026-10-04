"""Shared salary band definitions for all regions.

Single source of truth for salary expectation bands used across:
- apps/auto_apply/services/drafting.py (draft-time resolution)
- apps/web/forms.py (ExplicitAnswersForm choices)
- templates/web/profile_form.html (region tabs via json_script)
"""

# Salary bands per currency/region
SALARY_BANDS_BY_REGION = {
    "US": [
        ("", "— Select —"),
        ("<50k", "< $50,000"),
        ("50-75k", "$50,000 – $75,000"),
        ("75-100k", "$75,000 – $100,000"),
        ("100-125k", "$100,000 – $125,000"),
        ("125-150k", "$125,000 – $150,000"),
        ("150-175k", "$150,000 – $175,000"),
        ("175-200k", "$175,000 – $200,000"),
        ("200-250k", "$200,000 – $250,000"),
        ("250-300k", "$250,000 – $300,000"),
        ("300-400k", "$300,000 – $400,000"),
        ("400k+", "$400,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "EU": [
        ("", "— Select —"),
        ("<40k", "< €40,000"),
        ("40-55k", "€40,000 – €55,000"),
        ("55-70k", "€55,000 – €70,000"),
        ("70-90k", "€70,000 – €90,000"),
        ("90-120k", "€90,000 – €120,000"),
        ("120-150k", "€120,000 – €150,000"),
        ("150-200k", "€150,000 – €200,000"),
        ("200k+", "€200,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "IN": [
        ("", "— Select —"),
        ("<10L", "< ₹10 LPA"),
        ("10-15L", "₹10 – 15 LPA"),
        ("15-25L", "₹15 – 25 LPA"),
        ("25-35L", "₹25 – 35 LPA"),
        ("35-50L", "₹35 – 50 LPA"),
        ("50-70L", "₹50 – 70 LPA"),
        ("70-100L", "₹70 – 100 LPA"),
        ("100L+", "₹1 Cr+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "UK": [
        ("", "— Select —"),
        ("<35k", "< £35,000"),
        ("35-45k", "£35,000 – £45,000"),
        ("45-60k", "£45,000 – £60,000"),
        ("60-80k", "£60,000 – £80,000"),
        ("80-100k", "£80,000 – £100,000"),
        ("100-130k", "£100,000 – £130,000"),
        ("130-160k", "£130,000 – £160,000"),
        ("160k+", "£160,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "CA": [
        ("", "— Select —"),
        ("<60k", "< C$60,000"),
        ("60-80k", "C$60,000 – C$80,000"),
        ("80-100k", "C$80,000 – C$100,000"),
        ("100-130k", "C$100,000 – C$130,000"),
        ("130-160k", "C$130,000 – C$160,000"),
        ("160-200k", "C$160,000 – C$200,000"),
        ("200k+", "C$200,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "AU": [
        ("", "— Select —"),
        ("<70k", "< A$70,000"),
        ("70-90k", "A$70,000 – A$90,000"),
        ("90-120k", "A$90,000 – A$120,000"),
        ("120-150k", "A$120,000 – A$150,000"),
        ("150-180k", "A$150,000 – A$180,000"),
        ("180-220k", "A$180,000 – A$220,000"),
        ("220k+", "A$220,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
    "SG": [
        ("", "— Select —"),
        ("<60k", "< S$60,000"),
        ("60-80k", "S$60,000 – S$80,000"),
        ("80-110k", "S$80,000 – S$110,000"),
        ("110-140k", "S$110,000 – S$140,000"),
        ("140-180k", "S$140,000 – S$180,000"),
        ("180-220k", "S$180,000 – S$220,000"),
        ("220k+", "S$220,000+"),
        ("negotiable", "Negotiable / Open to market range"),
        ("other", "Other (specify in notes)"),
    ],
}

DEFAULT_SALARY_BANDS = SALARY_BANDS_BY_REGION["US"]


def get_salary_band_label(region: str, band_key: str) -> str:
    """Get the display label for a salary band key in a region."""
    bands = SALARY_BANDS_BY_REGION.get(region, DEFAULT_SALARY_BANDS)
    for key, label in bands:
        if key == band_key:
            return label
    return band_key


def is_valid_band_key(region: str, band_key: str) -> bool:
    """Check if a band key is valid for a region."""
    bands = SALARY_BANDS_BY_REGION.get(region, DEFAULT_SALARY_BANDS)
    return any(key == band_key for key, _ in bands)


def get_all_band_keys() -> set[str]:
    """Get all valid band keys across all regions."""
    keys = set()
    for bands in SALARY_BANDS_BY_REGION.values():
        keys.update(key for key, _ in bands)
    return keys


def validate_salary_by_region(data: dict) -> dict:
    """Validate and clean salary_by_region dict from user input.
    
    Returns a cleaned dict with only known regions and valid band keys.
    """
    if not isinstance(data, dict):
        return {}
    
    cleaned = {}
    for region, band_key in data.items():
        if region in SALARY_BANDS_BY_REGION and is_valid_band_key(region, band_key):
            cleaned[region] = band_key
    return cleaned
