"""IMAP-backed EmailCodeProvider implementation."""
from datetime import datetime, timezone
import imaplib
import logging
import socket
import ssl
import time
from typing import TYPE_CHECKING

from django.conf import settings

from apps.accounts.crypto import SecretDecryptionError, decrypt_secret

from .base import CodeLookupResult, EmailCodeProvider, VerificationOutcome
from .extraction import evaluate_email_candidate

if TYPE_CHECKING:
    from apps.accounts.models import EmailInboxCredential

logger = logging.getLogger(__name__)


def build_email_code_provider(user) -> EmailCodeProvider | None:
    """Build an active EmailCodeProvider for `user`, or None if unconfigured/inactive."""
    if not hasattr(user, "email_inbox_credential"):
        return None

    credential = user.email_inbox_credential
    if not credential or not credential.is_active:
        return None

    return ImapEmailCodeProvider(credential)


class ImapEmailCodeProvider:
    """IMAP client polling an inbox for verification codes."""

    def __init__(self, credential: "EmailInboxCredential"):
        self.credential = credential

    def get_code(
        self, *, since: datetime, deadline_monotonic: float
    ) -> CodeLookupResult:
        """Poll inbox for verification code matching criteria until deadline."""
        if not self.credential.is_active:
            return CodeLookupResult(outcome=VerificationOutcome.NO_INBOX_CREDENTIALS)

        # Decrypt password
        try:
            raw_password = decrypt_secret(self.credential.app_password_encrypted)
        except (SecretDecryptionError, Exception) as exc:
            logger.error(
                "Failed to decrypt IMAP app password for user_id=%s: %s",
                self.credential.user_id,
                type(exc).__name__,
            )
            return CodeLookupResult(outcome=VerificationOutcome.INBOX_AUTH_FAILED)

        # Determine effective poll parameters from settings
        poll_interval = getattr(settings, "AUTO_APPLY_VERIFICATION_POLL_INTERVAL_SECONDS", 4)
        sender_allowlist = getattr(settings, "AUTO_APPLY_VERIFICATION_SENDER_ALLOWLIST", ["greenhouse.io"])

        imap_client = None
        try:
            # Connect to IMAP server with a 10s socket timeout
            ssl_context = ssl.create_default_context()
            imap_client = imaplib.IMAP4_SSL(
                host=self.credential.imap_host,
                port=self.credential.imap_port,
                ssl_context=ssl_context,
                timeout=10.0,
            )
        except (socket.error, OSError, ssl.SSLError) as exc:
            logger.warning(
                "IMAP connection failed for user_id=%s host=%s: %s",
                self.credential.user_id,
                self.credential.imap_host,
                type(exc).__name__,
            )
            return CodeLookupResult(outcome=VerificationOutcome.INBOX_UNAVAILABLE)

        # Login
        try:
            imap_client.login(self.credential.email_address, raw_password)
        except imaplib.IMAP4.error as exc:
            logger.warning(
                "IMAP login rejected for user_id=%s host=%s",
                self.credential.user_id,
                self.credential.imap_host,
            )
            try:
                imap_client.logout()
            except Exception:
                pass
            # R7: Auth failure deactivates credential
            self.credential.mark_auth_failed("inbox_auth_failed")
            return CodeLookupResult(outcome=VerificationOutcome.INBOX_AUTH_FAILED)
        except (socket.error, OSError, ssl.SSLError) as exc:
            logger.warning(
                "IMAP socket error during login for user_id=%s: %s",
                self.credential.user_id,
                type(exc).__name__,
            )
            try:
                imap_client.logout()
            except Exception:
                pass
            return CodeLookupResult(outcome=VerificationOutcome.INBOX_UNAVAILABLE)

        try:
            # Select inbox in read-only mode (D2)
            status, _ = imap_client.select("INBOX", readonly=True)
            if status != "OK":
                return CodeLookupResult(outcome=VerificationOutcome.INBOX_UNAVAILABLE)

            # Convert since for IMAP SINCE query format (e.g. 04-Aug-2026)
            since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
            imap_since_str = since_utc.strftime("%d-%b-%Y")

            while time.monotonic() < deadline_monotonic:
                try:
                    # CRITICAL (D2): Must execute NOOP before SEARCH to force server sync!
                    imap_client.noop()

                    # Search messages from today onwards
                    search_status, data = imap_client.search(None, f'(SINCE "{imap_since_str}")')
                    if search_status == "OK" and data and data[0]:
                        msg_ids = data[0].split()
                        # Inspect newest messages first (limit to 15 newest to bound I/O)
                        recent_ids = msg_ids[-15:]

                        found_codes: list[str] = []
                        for msg_id in reversed(recent_ids):
                            fetch_status, msg_data = imap_client.fetch(msg_id, "(BODY.PEEK[])")
                            if fetch_status != "OK" or not msg_data:
                                continue

                            for response_part in msg_data:
                                if isinstance(response_part, tuple) and len(response_part) > 1:
                                    msg_bytes = response_part[1]
                                    if isinstance(msg_bytes, bytes):
                                        code = evaluate_email_candidate(
                                            msg_bytes, since, sender_allowlist
                                        )
                                        if code:
                                            found_codes.append(code)

                        if found_codes:
                            unique_codes = set(found_codes)
                            if len(unique_codes) == 1:
                                matched_code = found_codes[0]
                                logger.info(
                                    "Verification code found via IMAP for user_id=%s",
                                    self.credential.user_id,
                                )
                                return CodeLookupResult(
                                    outcome=VerificationOutcome.FOUND, code=matched_code
                                )
                            else:
                                logger.warning(
                                    "Ambiguous verification codes found for user_id=%s",
                                    self.credential.user_id,
                                )
                                return CodeLookupResult(
                                    outcome=VerificationOutcome.CODE_AMBIGUOUS
                                )

                except (socket.error, OSError, imaplib.IMAP4.error) as exc:
                    logger.warning(
                        "IMAP error during poll loop for user_id=%s: %s",
                        self.credential.user_id,
                        type(exc).__name__,
                    )
                    return CodeLookupResult(outcome=VerificationOutcome.INBOX_UNAVAILABLE)

                # Sleep interval checking deadline
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))

            return CodeLookupResult(outcome=VerificationOutcome.CODE_TIMEOUT)

        finally:
            try:
                imap_client.close()
            except Exception:
                pass
            try:
                imap_client.logout()
            except Exception:
                pass
