"""Tests for ImapEmailCodeProvider."""
from datetime import datetime, timezone
import imaplib
import socket
import time
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


# -- Hang Scenario Tests (RH1-RH4) ----------------------------------------
#
# These tests verify that the provider fails closed (returns CODE_TIMEOUT or
# INBOX_UNAVAILABLE) within bounded time, rather than hanging indefinitely
# when IMAP operations are slow or unresponsive.


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
class ImapEmailCodeProviderHangScenarioTests(TestCase):
    """Test that various hang scenarios are caught and fail closed."""

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

    @patch("imaplib.IMAP4_SSL")
    def test_deadline_exceeded_before_connection_returns_code_timeout(self, mock_imap_cls):
        """RH1: If deadline is already exceeded when get_code() is called,
        return CODE_TIMEOUT immediately without attempting connection."""
        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)

        # Deadline is in the past (already expired)
        expired_deadline = time.monotonic() - 100.0

        result = provider.get_code(since=since, deadline_monotonic=expired_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)
        # IMAP4_SSL should NOT have been called
        mock_imap_cls.assert_not_called()

    @patch("imaplib.IMAP4_SSL")
    def test_connection_timeout_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP connection times out (socket.timeout), return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap_cls.side_effect = socket.timeout("Connection timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_login_timeout_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP login times out (socket.timeout), return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.side_effect = socket.timeout("Login timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)
        # Logout should be called to clean up
        mock_imap.logout.assert_called()

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_select_timeout_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP select (mailbox selection) times out, return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.side_effect = socket.timeout("Select timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_noop_timeout_in_poll_loop_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP noop times out during the poll loop, return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"1"])
        # First poll iteration: noop times out
        mock_imap.noop.side_effect = socket.timeout("Noop timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_search_timeout_in_poll_loop_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP search times out during the poll loop, return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.noop.return_value = ("OK", [b"OK"])
        # First poll iteration: search times out
        mock_imap.search.side_effect = socket.timeout("Search timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_fetch_timeout_in_poll_loop_returns_code_timeout(self, mock_imap_cls):
        """RH2: If IMAP fetch times out during the poll loop, return
        CODE_TIMEOUT rather than INBOX_UNAVAILABLE."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.noop.return_value = ("OK", [b"OK"])
        mock_imap.search.return_value = ("OK", [b"1"])
        # First poll iteration: fetch times out
        mock_imap.fetch.side_effect = socket.timeout("Fetch timed out")

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        future_deadline = time.monotonic() + 30.0

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_deadline_check_before_login(self, mock_imap_cls):
        """RH2: Deadline is checked before login(), so if the connection
        itself consumed most of the deadline budget, we fail closed early."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        # Simulate connection taking time by not setting up login response
        # The deadline check before login should catch this

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)

        # Very tight deadline - 0.1 seconds in the future
        future_deadline = time.monotonic() + 0.1

        # Let connection "take time" by sleeping before checking deadline
        def slow_connection(*args, **kwargs):
            time.sleep(0.2)
            return mock_imap
        mock_imap_cls.side_effect = slow_connection

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        # Should timeout waiting for connection to complete
        self.assertIn(
            result.outcome,
            [VerificationOutcome.CODE_TIMEOUT, VerificationOutcome.INBOX_UNAVAILABLE],
        )

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_deadline_expired_during_poll_loop(self, mock_imap_cls):
        """RH1: Deadline check happens at the start of each poll loop
        iteration, so if deadline expires during sleep, next iteration
        catches it and returns CODE_TIMEOUT."""
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap
        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.noop.return_value = ("OK", [b"OK"])
        mock_imap.search.return_value = ("OK", [b""])  # Empty result

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)

        # Tight deadline that will expire during the sleep between polls
        future_deadline = time.monotonic() + 0.5

        result = provider.get_code(since=since, deadline_monotonic=future_deadline)

        # Should timeout, not hang forever trying to find code
        self.assertEqual(result.outcome, VerificationOutcome.CODE_TIMEOUT)

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_KEY])
    @patch("imaplib.IMAP4_SSL")
    def test_socket_timeout_preserved_through_operations(self, mock_imap_cls):
        """Verify that socket timeout is properly passed to IMAP4_SSL
        constructor, capped to remaining deadline budget."""
        mock_imap_cls.return_value = MagicMock()

        provider = ImapEmailCodeProvider(self.credential)
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)

        # Deadline with ~15 seconds remaining
        future_deadline = time.monotonic() + 15.0

        # This will fail at login since we didn't configure mock_imap properly,
        # but we can check that the timeout parameter was passed correctly
        try:
            provider.get_code(since=since, deadline_monotonic=future_deadline)
        except Exception:
            pass

        # Check that IMAP4_SSL was called with timeout parameter
        self.assertTrue(mock_imap_cls.called)
        call_kwargs = mock_imap_cls.call_args[1]
        self.assertIn("timeout", call_kwargs)
        # Timeout should be capped at 10s but at least 3s
        timeout = call_kwargs["timeout"]
        self.assertGreaterEqual(timeout, 3.0)
        self.assertLessEqual(timeout, 10.0)
