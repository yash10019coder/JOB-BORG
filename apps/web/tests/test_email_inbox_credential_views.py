"""Tests for EmailInboxCredential views and forms."""
import imaplib
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.crypto import decrypt_secret
from apps.accounts.models import EmailInboxCredential

User = get_user_model()
TEST_KEY = Fernet.generate_key().decode()


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
class EmailInboxCredentialViewTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username="usera", password="pw")
        self.user_b = User.objects.create_user(username="userb", password="pw")
        self.url = reverse("email_inbox_credential")
        self.delete_url = reverse("delete_email_inbox_credential")

    def test_anonymous_redirect(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    @patch("imaplib.IMAP4_SSL")
    def test_valid_save_creates_credential(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])

        self.client.login(username="usera", password="pw")
        response = self.client.post(
            self.url,
            {
                "email_address": "usera@gmail.com",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "app_password": "abcd efgh ijkl mnop",
            },
        )
        self.assertRedirects(response, self.url)

        cred = EmailInboxCredential.objects.get(user=self.user_a)
        self.assertTrue(cred.is_active)
        self.assertEqual(cred.email_address, "usera@gmail.com")
        self.assertEqual(decrypt_secret(cred.app_password_encrypted), "abcdefghijklmnop")

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[])
    @patch("imaplib.IMAP4_SSL")
    def test_missing_encryption_config_saves_nothing_and_shows_an_error(self, mock_imap_cls):
        mock_imap_cls.return_value.login.return_value = ("OK", [b"Logged in"])
        self.client.login(username="usera", password="pw")

        response = self.client.post(
            self.url,
            {
                "email_address": "usera@gmail.com",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "app_password": "abcd efgh ijkl mnop",
            },
        )

        self.assertEqual(response.status_code, 200)   # re-rendered with an error, not a 500
        self.assertFalse(EmailInboxCredential.objects.filter(user=self.user_a).exists())

    @patch("imaplib.IMAP4_SSL")
    def test_failed_imap_login_shows_error(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.side_effect = imaplib.IMAP4.error("AUTH FAILED")

        self.client.login(username="usera", password="pw")
        response = self.client.post(
            self.url,
            {
                "email_address": "usera@gmail.com",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "app_password": "bad password 1234",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "app_password",
            "Could not authenticate with IMAP server. Make sure this is an App Password (not your main account password) and 2-Step Verification is enabled.",
        )
        self.assertFalse(EmailInboxCredential.objects.filter(user=self.user_a).exists())

    @patch("imaplib.IMAP4_SSL")
    def test_delete_credential(self, mock_imap_cls):
        cred = EmailInboxCredential.objects.create(
            user=self.user_a,
            email_address="usera@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        cred.set_app_password("abcdefghijklmnop")

        self.client.login(username="usera", password="pw")
        response = self.client.post(self.delete_url)
        self.assertRedirects(response, self.url)
        self.assertFalse(EmailInboxCredential.objects.filter(pk=cred.pk).exists())

    @patch("imaplib.IMAP4_SSL")
    def test_user_b_cannot_delete_user_a_credential(self, mock_imap_cls):
        cred_a = EmailInboxCredential.objects.create(
            user=self.user_a,
            email_address="usera@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        cred_a.set_app_password("abcdefghijklmnop")

        self.client.login(username="userb", password="pw")
        response = self.client.post(self.delete_url)
        self.assertRedirects(response, self.url)
        # User A's credential must still exist
        self.assertTrue(EmailInboxCredential.objects.filter(pk=cred_a.pk).exists())

    @patch("imaplib.IMAP4_SSL")
    def test_rendered_html_never_contains_password_or_ciphertext(self, mock_imap_cls):
        cred = EmailInboxCredential.objects.create(
            user=self.user_a,
            email_address="usera@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        cred.set_app_password("secretpassword12")

        self.client.login(username="usera", password="pw")
        response = self.client.get(self.url)
        content = response.content.decode()

        self.assertNotIn("secretpassword12", content)
        self.assertNotIn(cred.app_password_encrypted, content)

    @patch("imaplib.IMAP4_SSL")
    def test_unreadable_saved_password_requires_replacement(self, mock_imap_cls):
        cred = EmailInboxCredential.objects.create(
            user=self.user_a, email_address="usera@gmail.com", imap_host="imap.gmail.com",
            app_password_encrypted="unreadable", is_active=False,
        )
        self.client.force_login(self.user_a)
        response = self.client.post(self.url, {
            "email_address": "changed@gmail.com", "imap_host": "imap.gmail.com", "imap_port": 993,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("app_password", response.context["form"].errors)
        mock_imap_cls.assert_not_called()
        cred.refresh_from_db()
        self.assertEqual(cred.email_address, "usera@gmail.com")
        self.assertFalse(cred.is_active)

    @patch("imaplib.IMAP4_SSL")
    def test_encryption_configuration_error_rolls_back_create_and_update(self, mock_imap_cls):
        self.client.force_login(self.user_a)
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    cred = EmailInboxCredential.objects.create(
                        user=self.user_a, email_address="old@gmail.com", imap_host="imap.gmail.com",
                        app_password_encrypted="old-ciphertext", is_active=False,
                    )
                with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[]):
                    response = self.client.post(self.url, {
                        "email_address": "new@gmail.com", "imap_host": "imap.gmail.com",
                        "imap_port": 993, "app_password": "abcdefghijklmnop",
                    })
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "credential encryption is not configured correctly")
                self.assertNotContains(response, "updated and verified successfully")
                if existing:
                    cred.refresh_from_db()
                    self.assertEqual(cred.email_address, "old@gmail.com")
                    self.assertEqual(cred.app_password_encrypted, "old-ciphertext")
                    self.assertFalse(cred.is_active)
                else:
                    self.assertFalse(EmailInboxCredential.objects.filter(user=self.user_a).exists())
