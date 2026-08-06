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
            "phone",
            "linkedin_url",
            "resume",
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
                    app_password = None
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

