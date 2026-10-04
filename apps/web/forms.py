"""Forms for the web UI.

The profile forms live in ``apps.web.forms_profile``.
"""
from django import forms


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
