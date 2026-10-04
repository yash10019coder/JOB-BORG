"""Copy every ExplicitAnswer into the user's AnswerBank as a legacy row.

``ExplicitAnswer`` stored *form option codes* ("no_need_sponsorship",
"yes_h1b", a salary band key) chosen on a page that is being retired. The
resolver now reads AnswerBank, so each row becomes a locked, user-sourced
``legacy:*`` row holding a readable value plus the yes/no it means
(``source_detail.answer_bool``), which the resolver maps onto whatever options
a given form offers.

* work authorization: ``yes_*`` -> True, ``no_*`` -> False, ``other`` -> None
  (never answers anything).
* sponsorship: ``yes_*`` -> True (needs sponsorship), ``no`` -> False,
  ``other`` -> None.
* salary: a band key that belongs to exactly one region becomes that region's
  label plus ``region`` (a band is quoted in one region's currency, so it is
  only ever used for a job in that region); anything else is kept verbatim
  with ``region=None`` and never answers.
* text that is not one of the old codes is kept verbatim as ``free_text``.

Idempotent and never overwrites: a row that already exists is left alone. The
label tables are frozen here on purpose -- the constants they came from are
deleted with the old form. The ExplicitAnswer table itself is kept, unwritten,
until Phase 5 drops it.
"""
from django.db import migrations

WORK_AUTH = {
    "yes_authorized": ("Yes, I am authorized to work in this country", True),
    "yes_h1b": ("Yes, on H-1B", True),
    "yes_opt": ("Yes, on OPT (F-1)", True),
    "yes_cpt": ("Yes, on CPT (F-1)", True),
    "yes_o1": ("Yes, on O-1", True),
    "yes_tn": ("Yes, on TN", True),
    "yes_e3": ("Yes, on E-3", True),
    "yes_green_card": ("Yes, I have a Green Card (permanent resident)", True),
    "no_sponsorship_needed": ("No, but I do not require sponsorship", False),
    "no_need_sponsorship": ("No, I require visa sponsorship", False),
    "other": ("Other", None),
}
SPONSORSHIP = {
    "no": ("No, I do not require sponsorship", False),
    "yes_h1b": ("Yes, H-1B", True),
    "yes_opt": ("Yes, OPT (F-1)", True),
    "yes_cpt": ("Yes, CPT (F-1)", True),
    "yes_o1": ("Yes, O-1", True),
    "yes_tn": ("Yes, TN", True),
    "yes_e3": ("Yes, E-3", True),
    "yes_green_card_process": ("Yes, Green Card process (PERM/I-140)", True),
    "other": ("Other", None),
}

KEYS = {
    "work_authorization": ("legacy:work_authorization", "t0_legal"),
    "sponsorship": ("legacy:sponsorship", "t0_legal"),
    "salary_expectation": ("legacy:salary_expectation", "t1_commercial"),
    # Kept for display; no question ever matches this key.
    "other": ("legacy:other", "t2_factual"),
}


def _salary_value(code):
    """``(value, region)`` for a salary band key."""
    from apps.accounts.salary_bands import SALARY_BANDS_BY_REGION, get_salary_band_label

    regions = [
        region
        for region, bands in SALARY_BANDS_BY_REGION.items()
        if code not in ("", "other", "negotiable") and any(key == code for key, _ in bands)
    ]
    if len(regions) == 1:
        return get_salary_band_label(regions[0], code), regions[0]
    return code, None


def translate(category, answer_text):
    """``(value, source_detail_extras)`` for one legacy row."""
    text = (answer_text or "").strip()
    if category in ("work_authorization", "sponsorship"):
        table = WORK_AUTH if category == "work_authorization" else SPONSORSHIP
        if text in table:
            label, boolean = table[text]
            return label, {"legacy_code": text, "answer_bool": boolean}
        return text, {"free_text": True, "answer_bool": None}
    if category == "salary_expectation":
        value, region = _salary_value(text)
        return value, {"legacy_code": text, "region": region}
    return text, {}


def backfill_explicit_answers(apps, schema_editor, user_ids=None):
    ExplicitAnswer = apps.get_model("auto_apply", "ExplicitAnswer")
    Profile = apps.get_model("accounts", "Profile")
    AnswerBank = apps.get_model("accounts", "AnswerBank")

    rows = ExplicitAnswer.objects.all()
    if user_ids is not None:
        rows = rows.filter(user_id__in=user_ids)
    for explicit in rows.iterator():
        category = explicit.category if explicit.category in KEYS else "other"
        profile, _ = Profile.objects.get_or_create(user_id=explicit.user_id)
        key, tier = KEYS[category]
        value, extras = translate(category, explicit.answer_text)
        AnswerBank.objects.get_or_create(
            profile=profile,
            question_key=key,
            scope_region="",
            defaults={
                "question_text": f"(saved answer: {category.replace('_', ' ')})",
                "value": value,
                "category": "other",
                "risk_tier": tier,
                "source": "user",
                "is_locked": True,
                "confidence": 1.0,
                "source_detail": {
                    "origin": "legacy_explicit_answer",
                    "legacy_category": explicit.category,
                    "explicit_answer_id": explicit.pk,
                    **extras,
                },
            },
        )


def noop_reverse(apps, schema_editor):
    """The backfilled rows are harmless; ExplicitAnswer is untouched."""


class Migration(migrations.Migration):

    dependencies = [
        ("auto_apply", "0010_autoapplydraft_submitted_answers_snapshot"),
        ("accounts", "0012_answerbank_scope_observation"),
    ]

    operations = [
        migrations.RunPython(backfill_explicit_answers, noop_reverse),
    ]
