"""Tests for ImapEmailCodeProvider."""
from datetime import datetime, timezone
import imaplib
import socket
from unittest.mock import MagicMock, call, patch

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


        msg_bytes = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Your Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 654321."
        )
        mock_imap.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [(b'1 (INTERNALDATE "05-Aug-2026 12:00:00 +0000" BODY[] {123}', msg_bytes)]),
        ]

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

        def mock_fetch(command, msg_id, spec):
            if command == "SEARCH":
                return ("OK", [b"1 2"])
            id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            if "1" in id_str:
                return ("OK", [(b'1 (INTERNALDATE "05-Aug-2026 12:00:00 +0000")', msg1)])
            return ("OK", [(b'2 (INTERNALDATE "05-Aug-2026 12:01:00 +0000")', msg2)])

        mock_imap.uid.side_effect = mock_fetch

        provider = ImapEmailCodeProvider(self.credential)
        result = provider.get_code(
            since=datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc),
            deadline_monotonic=1e9,
        )

        self.assertEqual(result.outcome, VerificationOutcome.CODE_AMBIGUOUS)

    @patch("imaplib.IMAP4_SSL")
    def test_login_abort_keeps_credential_active(self, mock_imap_cls):
        mock_imap_cls.return_value.login.side_effect = imaplib.IMAP4.abort("Connection closed")
        result = ImapEmailCodeProvider(self.credential).get_code(
            since=datetime.now(timezone.utc), deadline_monotonic=1e9
        )
        self.assertEqual(result.outcome, VerificationOutcome.INBOX_UNAVAILABLE)
        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_active)
        self.assertEqual(self.credential.last_error_code, "")
        mock_imap_cls.return_value.logout.assert_called_once()

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[])
    @patch("imaplib.IMAP4_SSL")
    def test_configuration_error_propagates(self, mock_imap_cls):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            ImapEmailCodeProvider(self.credential).get_code(
                since=datetime.now(timezone.utc), deadline_monotonic=1e9
            )
        mock_imap_cls.assert_not_called()

    @patch("imaplib.IMAP4_SSL")
    def test_bad_ciphertext_returns_auth_failure(self, mock_imap_cls):
        self.credential.app_password_encrypted = "invalid-token"
        result = ImapEmailCodeProvider(self.credential).get_code(
            since=datetime.now(timezone.utc), deadline_monotonic=1e9
        )
        self.assertEqual(result.outcome, VerificationOutcome.INBOX_AUTH_FAILED)
        mock_imap_cls.assert_not_called()

    @override_settings(AUTO_APPLY_VERIFICATION_SENDER_ALLOWLIST=["greenhouse.io", "jobs@example.com"])
    @patch("apps.auto_apply.email_verification.imap_provider.time.sleep")
    @patch("apps.auto_apply.email_verification.imap_provider.time.monotonic", side_effect=[0, 1, 2, 3, 11])
    @patch("imaplib.IMAP4_SSL")
    def test_uid_search_filters_senders_and_fetches_each_uid_once(self, mock_imap_cls, clock, sleep):
        imap = mock_imap_cls.return_value
        imap.select.return_value = ("OK", [b"2"])
        # This message has a convincing Date header but no usable INTERNALDATE.
        msg = b"From: no-reply@greenhouse.io\r\nDate: Wed, 05 Aug 2026 12:00:00 +0000\r\n\r\nYour verification code is 654321."
        imap.uid.side_effect = [
            ("OK", [b"10"]), ("OK", [(b'1 (UID 10 INTERNALDATE "invalid")', msg)]),
            ("OK", [b"10 11"]), ("OK", [(b"2 (UID 11)", msg)]),
        ]
        result = ImapEmailCodeProvider(self.credential).get_code(
            since=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc), deadline_monotonic=10
        )
        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)
        query = '(SINCE "04-Aug-2026" OR FROM "greenhouse.io" (FROM "jobs@example.com"))'
        self.assertEqual(imap.uid.call_args_list, [
            call("SEARCH", None, query), call("FETCH", b"10", "(INTERNALDATE BODY.PEEK[])"),
            call("SEARCH", None, query), call("FETCH", b"11", "(INTERNALDATE BODY.PEEK[])"),
        ])
        imap.search.assert_not_called()
        imap.fetch.assert_not_called()
