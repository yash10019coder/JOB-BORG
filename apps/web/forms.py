"""Forms for the web UI."""
from django import forms

from apps.accounts.models import Profile
from apps.locations.engine import CURRENT_LOCATION_ALIAS_VERSION, normalize_location

# Profile JSON list-fields edited as comma-separated text in the form.
_LIST_FIELDS = ("target_titles", "target_tags", "target_locations", "excluded_employers")


def _split_csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _normalize_target_locations(raw_locations):
    """Structured mirror of a raw target_locations list.

    One entry per raw string (order preserved, 1:1 with the raw list), deduped
    on the normalized (city, region, country) tuple so typing "NYC" and
    "New York" together doesn't double-count in hierarchy matching. Unresolved
    entries are kept (not dropped) -- scoring treats them as inert, and their
    presence is what would let a future UI flag "location not recognized".
    """
    seen_keys = set()
    normalized = []
    for raw in raw_locations:
        structured = normalize_location(raw)
        key = (structured["city"], structured["region"], structured["country"])
        if structured["resolved"] and key in seen_keys:
            continue
        if structured["resolved"]:
            seen_keys.add(key)
        normalized.append({"raw": raw, **structured})
    return normalized


class ProfileForm(forms.ModelForm):
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
            "full_name",
            "headline",
            "target_titles",
            "target_tags",
            "target_locations",
            "excluded_employers",
            "min_salary",
            "remote_pref",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Seed the CSV text inputs from the instance's stored lists.
        if self.instance and self.instance.pk:
            for field in _LIST_FIELDS:
                self.fields[field].initial = ", ".join(getattr(self.instance, field) or [])

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
        instance = super().save(commit=False)
        instance.target_locations_normalized = _normalize_target_locations(
            instance.target_locations
        )
        instance.target_locations_alias_version = CURRENT_LOCATION_ALIAS_VERSION
        if commit:
            instance.save()
        return instance
