"""Forms for the web UI."""
from django import forms

from apps.accounts.models import Profile
from apps.locations.engine import CURRENT_LOCATION_ALIAS_VERSION
from apps.locations.services import normalize_target_locations

# Profile JSON list-fields edited as comma-separated text in the form.
_LIST_FIELDS = ("target_titles", "target_tags", "target_locations", "excluded_employers")


def _split_csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


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
        instance.target_locations_normalized = normalize_target_locations(
            instance.target_locations
        )
        instance.target_locations_alias_version = CURRENT_LOCATION_ALIAS_VERSION
        if commit:
            instance.save()
        return instance
