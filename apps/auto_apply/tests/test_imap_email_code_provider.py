"""Tests for ImapEmailCodeProvider."""
from datetime import datetime, timezone
import imaplib
import socket
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.accounts.models import EmailInboxCredential
from apps.auto_apply.email_verification.base import (
    CodeLookupResult,
    EmailCodeProvider,
    VerificationOutcome,
)
from apps.auto_apply.email_verification.imap_provider import (
    ImapEmailCodeProvider,
    build_email_code_provider,
)

User = get_user_model()
TEST_KEY = Fernet.generate_key().decode()


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
class ImapEmailCodeProviderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pw")
        self.credential = EmailInboxCredential.objects.create(
            user=self.user,
            email_address="test@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        self.credential.set_app_password("abcdefghijklmnop")

    def test_satisfies_protocol(self):
        provider = ImapEmailCodeProvider(self.credential)
        self.assertIsInstance(provider, EmailCodeProvider)

    def test_build_email_code_provider_returns_none_if_no_credential(self):
        user_no_cred = User.objects.create_user(username="nocred", password="pw")
        self.assertIsNone(build_email_code_provider(user_no_cred))

    def test_build_email_code_provider_returns_none_if_inactive(self):
        self.credential.is_active = False
        self.credential.save()
        self.assertIsNone(build_email_code_provider(self.user))

    @patch("imaplib.IMAP4_SSL")
    def test_happy_path_code_found(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap

        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.noop.return_value = ("OK", [b"OK"])
        mock_imap.search.return_value = ("OK", [b"1"])

        msg_bytes = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Your Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 654321."
        )
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[PEEK[]] {123}", msg_bytes)])

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        result = provider.get_code(since=since, deadline_monotonic=1e9)

        self.assertEqual(result.outcome, VerificationOutcome.FOUND)
        self.assertEqual(result.code, "654321")
        mock_imap.select.assert_called_with("INBOX", readonly=True)
        mock_imap.noop.assert_called()

    @patch("imaplib.IMAP4_SSL")
    def test_auth_failure_deactivates_credential(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")

        provider = ImapEmailCodeProvider(self.credential)
        result = provider.get_code(
            since=datetime.now(timezone.utc), deadline_monotonic=1e9
        )

        self.assertEqual(result.outcome, VerificationOutcome.INBOX_AUTH_FAILED)
        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)
        self.assertEqual(self.credential.last_error_code, "inbox_auth_failed")

    @patch("imaplib.IMAP4_SSL")
    def test_socket_error_does_not_deactivate_credential(self, mock_imap_cls):
        mock_imap_cls.side_effect = socket.error("Connection refused")

        provider = ImapEmailCodeProvider(self.credential)
        result = provider.get_code(
            since=datetime.now(timezone.utc), deadline_monotonic=1e9
        )

        self.assertEqual(result.outcome, VerificationOutcome.INBOX_UNAVAILABLE)
        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_active)

    @patch("imaplib.IMAP4_SSL")
    def test_ambiguous_codes_returns_code_ambiguous(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"2"])
        mock_imap.noop.return_value = ("OK", [b"OK"])
        mock_imap.search.return_value = ("OK", [b"1 2"])

        msg1 = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Greenhouse verification code 1\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\nYour verification code is 111111."
        )
        msg2 = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Greenhouse verification code 2\r\n"
            b"Date: Wed, 05 Aug 2026 12:01:00 +0000\r\n"
            b"\r\nYour verification code is 222222."
        )

        def mock_fetch(msg_id, spec):
            id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            if "1" in id_str:
                return ("OK", [(b"1", msg1)])
            return ("OK", [(b"2", msg2)])

        mock_imap.fetch.side_effect = mock_fetch

        provider = ImapEmailCodeProvider(self.credential)
        result = provider.get_code(
            since=datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc),
            deadline_monotonic=1e9,
        )

        self.assertEqual(result.outcome, VerificationOutcome.CODE_AMBIGUOUS)
