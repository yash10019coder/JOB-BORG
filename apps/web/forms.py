"""Forms for the web UI."""
from itertools import zip_longest
from django import forms

from apps.accounts.models import Profile
from apps.auto_apply.models import ExplicitAnswer
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
    # Visa status per country (JSON), edited as comma-separated "US:h1b, CA:citizen"
    visa_status_by_country = forms.CharField(
        required=False, help_text="Comma-separated country:visa pairs, e.g. US:h1b, CA:citizen"
    )
    # Citizenship countries (JSON), edited as comma-separated ISO codes
    citizenship_countries = forms.CharField(
        required=False, help_text="Comma-separated ISO country codes, e.g. US, IN"
    )

    class Meta:
        model = Profile
        fields = [
            "full_name",
            "headline",
            "phone",
            "linkedin_url",
            "github_url",
            "portfolio_url",
            "current_employer",
            "resume",
            "target_titles",
            "target_tags",
            "target_locations",
            "excluded_employers",
            "min_salary",
            "remote_pref",
            "is_active",
            "visa_status_by_country",
            "citizenship_countries",
            "preferred_currency",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Seed the CSV text inputs from the instance's stored lists. Must go
        # through `self.initial` (form-level), not `self.fields[field].initial`
        # -- BaseModelForm.__init__ above already populated `self.initial`
        # from `model_to_dict(instance)` with the raw JSONField list, and
        # that takes precedence over `field.initial` when the widget resolves
        # its value, silently discarding a field-level-only assignment here.
        if self.instance and self.instance.pk:
            for field in _LIST_FIELDS:
                self.initial[field] = ", ".join(getattr(self.instance, field) or [])
        # `resume` writes must route through `Profile.set_resume()` (the one
        # call site that resets `resume_text` and enqueues parsing) rather
        # than the plain field assignment ModelForm.save() would otherwise
        # do -- stash the pre-edit value so save() can restore it before the
        # normal save, then apply the change via set_resume() separately.
        self._initial_resume = self.instance.resume if self.instance and self.instance.pk else None

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

    def clean_visa_status_by_country(self):
        """Parse repeater rows (visa_country[] / visa_status[]) or CSV fallback."""
        from apps.locations.engine import alpha3_for_country

        result = {}
        # Prefer repeater-style inputs from getlist
        data = self.data
        countries = data.getlist("visa_country[]")
        statuses = data.getlist("visa_status[]")
        if countries or statuses:
            # Build pairs by index; truncate to shorter list if mismatched
            pairs = list(zip_longest(countries, statuses, fillvalue=""))
            for country_raw, status_raw in pairs:
                country = (country_raw or "").strip()
                status = (status_raw or "").strip().lower()
                if not country or not status:
                    continue
                a3 = alpha3_for_country(country)
                if not a3:
                    # Don't guess; skip unrecognized, or let a later validator
                    # catch if needed. Store by what user gave as fallback? No.
                    continue
                result[a3] = status
            return result
        # CSV fallback
        raw = self.cleaned_data.get("visa_status_by_country", "")
        pairs = [p.strip() for p in raw.split(",") if p.strip()]
        for pair in pairs:
            if ":" not in pair:
                continue
            country, visa = pair.split(":", 1)
            country = country.strip()
            visa = visa.strip().lower()
            a3 = alpha3_for_country(country)
            if a3 and visa:
                result[a3] = visa
        return result

    def clean_citizenship_countries(self):
        """Parse citizenship multiselect or CSV into list of alpha-3 codes."""
        from apps.locations.engine import alpha3_for_country

        data = self.data
        # Repeater/multiselect style
        countries = data.getlist("citizenship_countries[]")
        if not countries:
            countries = data.getlist("citizenship_countries")
        if countries:
            out = []
            for c in countries:
                a3 = alpha3_for_country(c)
                if a3:
                    out.append(a3)
            # De-dup while preserving order
            seen = set()
            result = []
            for x in out:
                if x not in seen:
                    seen.add(x)
                    result.append(x)
            return result
        raw = self.cleaned_data.get("citizenship_countries", "")
        codes = [c.strip() for c in raw.split(",") if c.strip()]
        out = []
        for c in codes:
            a3 = alpha3_for_country(c)
            if a3:
                out.append(a3)
        return out

    def save(self, commit=True):
        resume_changed = "resume" in self.changed_data
        new_resume = self.cleaned_data.get("resume") if resume_changed else None
        instance = super().save(commit=False)
        if resume_changed:
            # Undo ModelForm's direct field assignment -- set_resume() below
            # is the one call site allowed to actually change `resume`.
            instance.resume = self._initial_resume
        instance.target_locations_normalized = normalize_target_locations(
            instance.target_locations
        )
        instance.target_locations_alias_version = CURRENT_LOCATION_ALIAS_VERSION
        if commit:
            instance.save()
            if resume_changed:
                instance.set_resume(new_resume)
        return instance


class EmailInboxCredentialForm(forms.ModelForm):
    app_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Google App Password (16 characters, e.g. abcd efgh ijkl mnop).",
    )

    class Meta:
        from apps.accounts.models import EmailInboxCredential

        model = EmailInboxCredential
        fields = ["email_address", "imap_host", "imap_port"]

    def clean_imap_host(self):
        from django.conf import settings

        host = self.cleaned_data.get("imap_host", "").strip().lower()
        allowed = getattr(settings, "AUTO_APPLY_IMAP_ALLOWED_HOSTS", ["imap.gmail.com"])
        allowed_hosts = [h.lower() for h in allowed]
        if host not in allowed_hosts:
            raise forms.ValidationError(
                f"IMAP host '{host}' is not in the allowed host list."
            )
        return host

    def clean(self):
        import imaplib
        import socket
        import ssl

        cleaned_data = super().clean()
        email_address = cleaned_data.get("email_address")
        imap_host = cleaned_data.get("imap_host")
        imap_port = cleaned_data.get("imap_port") or 993
        app_password = cleaned_data.get("app_password")

        # If editing existing credential and app_password is left blank, preserve existing
        if not app_password:
            if self.instance and self.instance.pk and self.instance.app_password_encrypted:
                from apps.accounts.crypto import decrypt_secret

                try:
                    app_password = decrypt_secret(self.instance.app_password_encrypted)
                except Exception:
                    self.add_error(
                        "app_password",
                        "The stored App Password could not be decrypted. Enter a new App Password.",
                    )
                    return cleaned_data
            else:
                self.add_error("app_password", "An App Password is required.")
                return cleaned_data

        if email_address and imap_host and app_password:
            stripped_password = app_password.replace(" ", "")
            try:
                ssl_context = ssl.create_default_context()
                client = imaplib.IMAP4_SSL(
                    host=imap_host,
                    port=imap_port,
                    ssl_context=ssl_context,
                    timeout=10.0,
                )
                try:
                    client.login(email_address, stripped_password)
                    try:
                        client.logout()
                    except Exception:
                        pass
                except imaplib.IMAP4.error:
                    self.add_error(
                        "app_password",
                        "Could not authenticate with IMAP server. Make sure this is an App Password (not your main account password) and 2-Step Verification is enabled.",
                    )
                except (socket.error, OSError, ssl.SSLError):
                    self.add_error(
                        "imap_host",
                        f"Could not connect to IMAP server at {imap_host}:{imap_port}.",
                    )
            except Exception as exc:
                self.add_error(
                    "app_password",
                    f"IMAP verification failed: {type(exc).__name__}",
                )

        return cleaned_data


# Common Greenhouse options for work authorization questions
WORK_AUTH_CHOICES = [
    ("", "— Select —"),
    ("yes_authorized", "Yes, I am authorized to work in this country"),
    ("yes_h1b", "Yes, on H-1B"),
    ("yes_opt", "Yes, on OPT (F-1)"),
    ("yes_cpt", "Yes, on CPT (F-1)"),
    ("yes_o1", "Yes, on O-1"),
    ("yes_tn", "Yes, on TN"),
    ("yes_e3", "Yes, on E-3"),
    ("yes_green_card", "Yes, I have a Green Card (permanent resident)"),
    ("no_sponsorship_needed", "No, but I do not require sponsorship"),
    ("no_need_sponsorship", "No, I require visa sponsorship"),
    ("other", "Other"),
]

SPONSORSHIP_CHOICES = [
    ("", "— Select —"),
    ("no", "No, I do not require sponsorship"),
    ("yes_h1b", "Yes, H-1B"),
    ("yes_opt", "Yes, OPT (F-1)"),
    ("yes_cpt", "Yes, CPT (F-1)"),
    ("yes_o1", "Yes, O-1"),
    ("yes_tn", "Yes, TN"),
    ("yes_e3", "Yes, E-3"),
    ("yes_green_card_process", "Yes, Green Card process (PERM/I-140)"),
    ("other", "Other"),
]

from apps.web.salary_bands import SALARY_BANDS_BY_REGION, DEFAULT_SALARY_BANDS, get_all_band_keys


class ExplicitAnswersForm(forms.Form):
    work_authorization = forms.ChoiceField(
        choices=WORK_AUTH_CHOICES, required=False, label="Work authorization"
    )
    sponsorship = forms.ChoiceField(
        choices=SPONSORSHIP_CHOICES, required=False, label="Sponsorship"
    )
    salary_expectation = forms.ChoiceField(
        choices=[(k, k) for k in sorted(get_all_band_keys())], required=False, label="Salary expectation"
    )
    other = forms.CharField(
        widget=forms.Textarea, required=False, label="Other"
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        initial = kwargs.pop("initial", {}) or {}
        if user is not None:
            # Load from ExplicitAnswer DB rows
            for ea in ExplicitAnswer.objects.filter(user=user):
                initial[ea.category] = ea.answer_text
            # Autofill from Profile if no explicit answer exists yet
            profile = getattr(user, "profile", None)
            if profile:
                if "salary_expectation" not in initial and profile.min_salary:
                    initial["salary_expectation"] = self._salary_to_band(profile.min_salary)
                if "work_authorization" not in initial and profile.visa_status_by_country:
                    initial["work_authorization"] = self._visa_status_to_auth(profile.visa_status_by_country)
                if "sponsorship" not in initial and profile.visa_status_by_country:
                    initial["sponsorship"] = self._visa_status_to_sponsorship(profile.visa_status_by_country)
        # Ensure all fields have an initial value (empty string if not set)
        # This ensures ChoiceFields render with the "— Select —" option
        for category, _label in ExplicitAnswer.Category.choices:
            initial.setdefault(category, "")
        super().__init__(*args, initial=initial, **kwargs)

    @staticmethod
    def _salary_to_band(min_salary: int) -> str:
        if min_salary < 50000: return "<50k"
        if min_salary < 75000: return "50-75k"
        if min_salary < 100000: return "75-100k"
        if min_salary < 125000: return "100-125k"
        if min_salary < 150000: return "125-150k"
        if min_salary < 175000: return "150-175k"
        if min_salary < 200000: return "175-200k"
        if min_salary < 250000: return "200-250k"
        if min_salary < 300000: return "250-300k"
        if min_salary < 400000: return "300-400k"
        return "400k+"

    @staticmethod
    def _visa_status_to_auth(visa_status_by_country: dict | str | None) -> str:
        if isinstance(visa_status_by_country, dict):
            # Autofill only when unambiguous across all countries, or when
            # the single stored US entry is clearly one of the mapped values.
            if len(visa_status_by_country) == 1:
                status = next(iter(visa_status_by_country.values()))
            else:
                # Mixed statuses: do not guess.
                return ""
            mapping = {
                "citizen": "yes_authorized",
                "permanent_resident": "yes_green_card",
                "work_permit": "yes_authorized",
                "requires_sponsorship": "no_need_sponsorship",
                "not_authorized": "no_sponsorship_needed",
                "h1b": "yes_h1b",
                "opt": "yes_opt",
                "o1": "yes_o1",
                "tn": "yes_tn",
                "e3": "yes_e3",
                "other": "other",
            }
            return mapping.get(status, "")
        # Legacy string (very old rows)
        mapping = {
            "citizen": "yes_authorized",
            "permanent_resident": "yes_green_card",
            "h1b": "yes_h1b",
            "opt": "yes_opt",
            "cpt": "yes_cpt",
            "o1": "yes_o1",
            "tn": "yes_tn",
            "e3": "yes_e3",
            "not_authorized": "no_sponsorship_needed",
        }
        return mapping.get((visa_status_by_country or "").lower(), "")

    @staticmethod
    def _visa_status_to_sponsorship(visa_status_by_country: dict | str | None) -> str:
        if isinstance(visa_status_by_country, dict):
            if len(visa_status_by_country) == 1:
                status = next(iter(visa_status_by_country.values()))
            else:
                return ""
            # Only return a sponsorship answer for statuses that clearly indicate a response
            # Ambiguous statuses should not autofill sponsorship
            mapping = {
                "citizen": "no",
                "permanent_resident": "no",
                "requires_sponsorship": "other",
                "h1b": "yes_h1b",
                "o1": "yes_o1",
                "tn": "yes_tn",
                "e3": "yes_e3",
            }
            return mapping.get(status, "")
        mapping = {
            "citizen": "no",
            "permanent_resident": "no",
            "h1b": "yes_h1b",
            "o1": "yes_o1",
            "tn": "yes_tn",
            "e3": "yes_e3",
            "green_card_process": "yes_green_card_process",
        }
        return mapping.get((visa_status_by_country or "").lower(), "")

    def save(self):
        from django.db import transaction
        with transaction.atomic():
            for category, _label in ExplicitAnswer.Category.choices:
                value = self.cleaned_data.get(category, "")
                if value:
                    ExplicitAnswer.objects.update_or_create(
                        user=self.user, category=category, defaults={"answer_text": value}
                    )
                else:
                    ExplicitAnswer.objects.filter(user=self.user, category=category).delete()

