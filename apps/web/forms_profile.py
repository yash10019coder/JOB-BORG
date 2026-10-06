"""Forms for the two profile pages.

``/profile/`` (search criteria) and ``/profile/answers/`` (what auto-apply
fills into application forms) are separate pages with separate forms on
purpose: a save on one must never touch, blank or rematch because of fields
that live on the other. Each form lists exactly the columns it owns.
"""
from itertools import zip_longest
from zoneinfo import available_timezones

from django import forms

from apps.accounts import question_semantics
from apps.accounts.models import AnswerBank, Profile
from apps.accounts.regions import REGION_KEYS, REGION_LABELS, country_choices
from apps.accounts.salary_bands import SALARY_BANDS_BY_REGION
from apps.accounts.services import profile_fields
from apps.accounts.services.panel_cache import invalidate_questions_panel
from apps.accounts.tiering import Tier
from apps.locations.engine import (
    CURRENT_LOCATION_ALIAS_VERSION,
    alpha3_for_country,
)
from apps.locations.services import normalize_target_locations

# Profile JSON list-fields edited as comma-separated text in the form.
_LIST_FIELDS = ("target_titles", "target_tags", "target_locations", "excluded_employers")


def _split_csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def timezone_choices():
    """Blank plus every IANA zone with a region ("Asia/Kolkata") and UTC."""
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
    return [("", "— Select —"), ("UTC", "UTC")] + [(z, z) for z in zones]


class ProfileSearchForm(forms.ModelForm):
    """Search criteria and the resume: everything that decides which jobs match."""

    target_titles = forms.CharField(
        required=False,
        help_text="Comma-separated, e.g. Backend Engineer, Platform Engineer",
    )
    target_tags = forms.CharField(
        required=False, help_text="Comma-separated skills/keywords, e.g. python, kubernetes"
    )
    target_locations = forms.CharField(
        required=False, help_text="Comma-separated, e.g. New York, London"
    )
    excluded_employers = forms.CharField(
        required=False, help_text="Comma-separated employer slugs to hide"
    )

    class Meta:
        model = Profile
        fields = [
            "headline",
            "resume",
            "target_titles",
            "target_tags",
            "target_locations",
            "excluded_employers",
            "min_salary",
            "remote_pref",
            "is_active",
            "preferred_currency",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Seed the CSV text inputs from the stored lists. Must go through
        # `self.initial` (form-level): BaseModelForm already populated it from
        # `model_to_dict(instance)` with the raw list, and that takes
        # precedence over `field.initial`.
        if self.instance and self.instance.pk:
            for field in _LIST_FIELDS:
                self.initial[field] = ", ".join(getattr(self.instance, field) or [])
        # `resume` writes must route through `Profile.set_resume()` (the one
        # call site that resets `resume_text` and enqueues parsing) rather
        # than the plain assignment ModelForm.save() would do.
        self._initial_resume = self.instance.resume if self.instance and self.instance.pk else None
        self._covered_before = profile_fields.snapshot(self.instance)

    def _clean_list(self, field):
        return _split_csv(self.cleaned_data.get(field, ""))

    def clean_target_titles(self):
        return self._clean_list("target_titles")

    def clean_target_tags(self):
        return self._clean_list("target_tags")

    def clean_target_locations(self):
        return self._clean_list("target_locations")

    def clean_excluded_employers(self):
        return self._clean_list("excluded_employers")

    def save(self, commit=True):
        resume_changed = "resume" in self.changed_data
        new_resume = self.cleaned_data.get("resume") if resume_changed else None
        instance = super().save(commit=False)
        if resume_changed:
            # Undo ModelForm's direct assignment -- set_resume() below is the
            # one call site allowed to actually change `resume`.
            instance.resume = self._initial_resume
        instance.target_locations_normalized = normalize_target_locations(
            instance.target_locations
        )
        instance.target_locations_alias_version = CURRENT_LOCATION_ALIAS_VERSION
        # Same save as the field changes: provenance rides along, no second
        # post_save (and so no second rematch).
        profile_fields.record_user_edits(instance, self._covered_before)
        if commit:
            instance.save()  # a full save: any search change must rematch
            if resume_changed:
                instance.set_resume(new_resume)
        return instance


class AnswersSettingsForm(forms.ModelForm):
    """What auto-apply fills into application forms: contact and location,
    work authorization per country, citizenship, salary by region.

    Saves with explicit ``update_fields`` naming only these columns, so the
    save triggers no rematch (matching reads none of them).
    """

    UPDATE_FIELDS = (
        "full_name",
        "phone",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "current_employer",
        "location_city",
        "location_country",
        "mailing_address",
        "working_timezone",
        "visa_status_by_country",
        "citizenship_countries",
        "salary_by_region",
        "field_provenance",
        "updated_at",
    )

    location_country = forms.ChoiceField(
        required=False,
        label="Country you live in",
        help_text="Used to answer “Country”, “Where are you based?” and similar questions.",
    )
    mailing_address = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Full mailing address, up to 500 characters.",
    )
    working_timezone = forms.ChoiceField(required=False, label="Working time zone")
    # Citizenship is genuinely multi-valued (dual citizens). It is posted as one
    # `citizenship_countries` select per passport (a repeater, like the work
    # authorization rows), so the user sees exactly what is selected. A blank
    # choice is accepted and dropped in clean().
    citizenship_countries = forms.MultipleChoiceField(
        required=False,
        label="Countries whose passports you hold",
        help_text=(
            "Add a row for each passport you hold. This answers \u201cAre you a "
            "citizen of X?\u201d and nationality questions on application forms."
        ),
    )

    class Meta:
        model = Profile
        fields = [
            "full_name",
            "phone",
            "linkedin_url",
            "github_url",
            "portfolio_url",
            "current_employer",
            "location_city",
            "location_country",
            "mailing_address",
            "working_timezone",
        ]
        # visa_status_by_country / citizenship_countries / salary_by_region are
        # repeater- and select-shaped, parsed in clean() and assigned in save().

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location_country"].choices = [("", "— Select —")] + country_choices()
        self.fields["working_timezone"].choices = timezone_choices()
        self.fields["citizenship_countries"].choices = [("", "— Select country —")] + country_choices()
        if self.instance and self.instance.pk:
            self.initial["citizenship_countries"] = [
                c for c in (self.instance.citizenship_countries or []) if c
            ]
        stored_salary = (self.instance.salary_by_region or {}) if self.instance else {}
        for region_key in REGION_KEYS:
            field_name = f"salary_region_{region_key}"
            self.fields[field_name] = forms.ChoiceField(
                required=False,
                label=f"{REGION_LABELS[region_key]} salary expectation",
                choices=SALARY_BANDS_BY_REGION[region_key],
            )
            self.initial.setdefault(field_name, stored_salary.get(region_key, ""))
        self._covered_before = profile_fields.snapshot(self.instance)

    CONTACT_FIELDS = (
        "full_name",
        "phone",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "current_employer",
        "location_city",
        "location_country",
        "mailing_address",
        "working_timezone",
    )

    @property
    def contact_fields(self):
        return [self[name] for name in self.CONTACT_FIELDS]

    @property
    def salary_fields(self):
        return [self[f"salary_region_{key}"] for key in REGION_KEYS]

    def visa_rows(self):
        """``[(alpha3, status)]`` to render: what was just posted if the form
        was submitted (so a validation error keeps the user's rows), else what
        is stored. Rendered by the server so the page works without JS."""
        if self.is_bound and "visa_rows_present" in self.data:
            return [
                (c.strip(), s.strip())
                for c, s in zip_longest(
                    self.data.getlist("visa_country[]"),
                    self.data.getlist("visa_status[]"),
                    fillvalue="",
                )
                if (c or "").strip() or (s or "").strip()
            ]
        return list((self.instance.visa_status_by_country or {}).items())

    def citizenship_rows(self):
        """Country codes to render as rows: what was just posted if the
        repeater was submitted (so a validation error keeps the user's rows),
        else what is stored."""
        if self.is_bound and "citizenship_rows_present" in self.data:
            posted = self.data.getlist("citizenship_countries")
        else:
            posted = self.instance.citizenship_countries or []
        seen, rows = set(), []
        for code in posted:
            code = (code or "").strip()
            if code and code not in seen:
                seen.add(code)
                rows.append(code)
        return rows

    def clean_citizenship_countries(self):
        """Posted selects, blanks and repeats dropped.

        A POST without the ``citizenship_rows_present`` marker did not render
        the repeater (an old client, a hand-built request): keep what is
        stored rather than reading "no rows" as "clear them".
        """
        if "citizenship_rows_present" not in self.data:
            return list(self.instance.citizenship_countries or [])
        seen, codes = set(), []
        for code in self.cleaned_data.get("citizenship_countries") or []:
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes

    def clean_visa_status_by_country(self):
        """Per-country work authorization from the repeater rows.

        Validation is strict on purpose -- an unrecognized country or status is
        a form error rather than a silently dropped row, because a dropped row
        reads back as "nothing known about this country". A country listed
        twice is an error too, not last-wins: picking one of two statuses for
        the user is a guess about their immigration status.

        A POST without the ``visa_rows_present`` marker did not render the
        repeater at all (an old client, a hand-built request): keep what is
        stored rather than reading "no rows" as "clear them".
        """
        if "visa_rows_present" not in self.data:
            return dict(self.instance.visa_status_by_country or {})
        valid_statuses = {value for value, _ in Profile.VisaStatus.choices}
        result: dict[str, str] = {}
        for index, (raw_country, raw_status) in enumerate(
            zip_longest(
                self.data.getlist("visa_country[]"),
                self.data.getlist("visa_status[]"),
                fillvalue="",
            )
        ):
            country = (raw_country or "").strip()
            status = (raw_status or "").strip()
            if not country and not status:
                continue  # blank spare row
            errors = []
            alpha3 = alpha3_for_country(country) if country else None
            if not alpha3:
                errors.append(f"Row {index + 1}: {country or '(no country)'} is not a recognized country.")
            if status not in valid_statuses:
                errors.append(f"Row {index + 1}: {status or '(no status)'} is not a valid status.")
            if errors:
                raise forms.ValidationError(" ".join(errors))
            if alpha3 in result:
                raise forms.ValidationError(
                    f"{country} is listed more than once -- each country needs one status."
                )
            result[alpha3] = status
        return result

    def clean(self):
        cleaned_data = super().clean()
        try:
            cleaned_data["visa_status_by_country"] = self.clean_visa_status_by_country()
        except forms.ValidationError as exc:
            self.add_error(None, exc)
        cleaned_data.setdefault("citizenship_countries", [])
        salary_by_region = {}
        for region_key in REGION_KEYS:
            value = cleaned_data.get(f"salary_region_{region_key}") or ""
            if value:
                salary_by_region[region_key] = value
        cleaned_data["salary_by_region"] = salary_by_region
        cleaned_data.setdefault("visa_status_by_country", {})
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.visa_status_by_country = self.cleaned_data.get("visa_status_by_country") or {}
        instance.citizenship_countries = self.cleaned_data.get("citizenship_countries") or []
        instance.salary_by_region = self.cleaned_data.get("salary_by_region") or {}
        profile_fields.record_user_edits(instance, self._covered_before)
        if commit:
            # Explicit update_fields: matching reads none of these columns, so
            # this save must not schedule a rematch.
            instance.save(update_fields=list(self.UPDATE_FIELDS))
            invalidate_questions_panel(instance.user_id)
        return instance


_TIER_CHOICES = [
    ("", "Automatic"),
    (Tier.T1_COMMERCIAL, "Treat as commercial (T1)"),
    (Tier.T0_LEGAL, "Treat as legal (T0)"),
]


class CustomAnswerForm(forms.Form):
    """Create or edit one custom answer (an AnswerBank row owned by the user)."""

    question_text = forms.CharField(
        label="Question",
        max_length=500,
        min_length=3,
        widget=forms.TextInput(attrs={"placeholder": "e.g. How did you hear about us?"}),
    )
    value = forms.CharField(label="Your answer", max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))
    category = forms.ChoiceField(choices=AnswerBank.Category.choices, initial=AnswerBank.Category.OTHER)
    scope = forms.ChoiceField(
        required=False,
        label="Applies to",
        choices=[("", "Any location")] + [(key, REGION_LABELS[key]) for key in REGION_KEYS],
        help_text=(
            "Only matters for relocation / on-site wording: an answer for one "
            "region is never used for a job in another."
        ),
    )
    lock = forms.BooleanField(required=False, label="Lock (never overwritten by learning or imports)")
    min_tier = forms.ChoiceField(
        required=False,
        choices=_TIER_CHOICES,
        label="Risk level",
        help_text="You can raise how carefully an answer is treated, never lower it.",
    )

    def clean_question_text(self):
        text = " ".join(self.cleaned_data["question_text"].split())
        if len(text) < 3:
            raise forms.ValidationError("Enter the question as it appears on the form.")
        return text

    def clean_value(self):
        value = self.cleaned_data["value"].strip()
        if not value:
            raise forms.ValidationError("An answer cannot be blank.")
        return value

    def write_kwargs(self):
        """Keyword arguments for ``write_answer`` from the cleaned data."""
        cleaned = self.cleaned_data
        scope = cleaned.get("scope", "")
        sensitive = question_semantics.is_location_sensitive(cleaned["question_text"])
        return {
            "category": cleaned["category"],
            "is_locked": cleaned.get("lock", False),
            "scope_region": scope,
            "min_tier": cleaned.get("min_tier") or None,
            # A location-sensitive answer with no region chosen is the user's
            # explicit "any location": only then is it reused everywhere.
            "applies_everywhere": bool(sensitive and not scope),
        }
