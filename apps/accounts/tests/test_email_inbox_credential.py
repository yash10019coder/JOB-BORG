from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.crypto import decrypt_secret
from apps.accounts.models import EmailInboxCredential

User = get_user_model()

RAW_APP_PASSWORD_WITH_SPACES = "abcd efgh ijkl mnop"
STRIPPED_APP_PASSWORD = "abcdefghijklmnop"

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_ENCRYPTION_KEY])
class SetAppPasswordTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.credential = EmailInboxCredential.objects.create(
            user=self.user,
            email_address="alice@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )

    def test_round_trip_strips_whitespace(self):
        self.credential.set_app_password(RAW_APP_PASSWORD_WITH_SPACES)

        reloaded = EmailInboxCredential.objects.get(pk=self.credential.pk)
        self.assertEqual(
            decrypt_secret(reloaded.app_password_encrypted), STRIPPED_APP_PASSWORD
        )

    def test_ciphertext_at_rest_never_contains_plaintext(self):
        # R3's load-bearing assertion: read the raw column value straight
        # from the database (bypassing the ORM/model entirely) and confirm
        # the plaintext password substring never appears in it.
        self.credential.set_app_password(RAW_APP_PASSWORD_WITH_SPACES)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT app_password_encrypted FROM accounts_emailinboxcredential "
                "WHERE id = %s",
                [self.credential.pk],
            )
            (raw_column_value,) = cursor.fetchone()

        self.assertNotIn(STRIPPED_APP_PASSWORD, raw_column_value)
        self.assertNotIn(RAW_APP_PASSWORD_WITH_SPACES, raw_column_value)

    def test_reactivates_and_clears_last_error_code(self):
        self.credential.mark_auth_failed("inbox_auth_failed")
        self.assertFalse(self.credential.is_active)
        self.assertEqual(self.credential.last_error_code, "inbox_auth_failed")

        self.credential.set_app_password(RAW_APP_PASSWORD_WITH_SPACES)

        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_active)
        self.assertEqual(self.credential.last_error_code, "")


class MarkAuthFailedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bob", password="pw")
        self.credential = EmailInboxCredential.objects.create(
            user=self.user,
            email_address="bob@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )

    def test_marks_inactive_and_stamps_error(self):
        before = timezone.now()
        self.credential.mark_auth_failed("inbox_auth_failed")

        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_active)
        self.assertEqual(self.credential.last_error_code, "inbox_auth_failed")
        self.assertIsNotNone(self.credential.last_error_at)
        self.assertGreaterEqual(self.credential.last_error_at, before)


class UniquenessAndCascadeTests(TestCase):
    def test_second_credential_for_same_user_rejected(self):
        user = User.objects.create_user(username="carol", password="pw")
        EmailInboxCredential.objects.create(
            user=user,
            email_address="carol@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailInboxCredential.objects.create(
                user=user,
                email_address="carol-2@gmail.com",
                imap_host="imap.gmail.com",
                imap_port=993,
                app_password_encrypted="",
            )

    def test_two_different_users_credentials_are_independent(self):
        user_a = User.objects.create_user(username="dave", password="pw")
        user_b = User.objects.create_user(username="erin", password="pw")
        cred_a = EmailInboxCredential.objects.create(
            user=user_a,
            email_address="dave@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        cred_b = EmailInboxCredential.objects.create(
            user=user_b,
            email_address="erin@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )

        cred_a.mark_auth_failed("inbox_auth_failed")

        cred_b.refresh_from_db()
        self.assertTrue(cred_b.is_active)
        self.assertEqual(cred_b.last_error_code, "")

    def test_deleting_user_deletes_credential(self):
        user = User.objects.create_user(username="frank", password="pw")
        credential = EmailInboxCredential.objects.create(
            user=user,
            email_address="frank@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        credential_pk = credential.pk

        user.delete()

        self.assertFalse(EmailInboxCredential.objects.filter(pk=credential_pk).exists())


class HostAndPortValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="grace", password="pw")

    @override_settings(AUTO_APPLY_IMAP_ALLOWED_HOSTS=["imap.gmail.com"])
    def test_disallowed_host_fails_full_clean(self):
        credential = EmailInboxCredential(
            user=self.user,
            email_address="grace@gmail.com",
            imap_host="evil.example.com",
            imap_port=993,
            app_password_encrypted="",
        )
        with self.assertRaises(ValidationError):
            credential.full_clean()

    @override_settings(AUTO_APPLY_IMAP_ALLOWED_HOSTS=["imap.gmail.com"])
    def test_allowed_host_passes_full_clean(self):
        credential = EmailInboxCredential(
            user=self.user,
            email_address="grace@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="placeholder-ciphertext",
        )
        credential.full_clean()  # must not raise

    @override_settings(AUTO_APPLY_IMAP_ALLOWED_HOSTS=["imap.gmail.com"])
    def test_disallowed_port_fails_full_clean(self):
        credential = EmailInboxCredential(
            user=self.user,
            email_address="grace@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=143,
            app_password_encrypted="",
        )
        with self.assertRaises(ValidationError):
            credential.full_clean()

    @override_settings(AUTO_APPLY_IMAP_ALLOWED_HOSTS=["imap.gmail.com"])
    def test_allowed_port_passes_full_clean(self):
        credential = EmailInboxCredential(
            user=self.user,
            email_address="grace@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="placeholder-ciphertext",
        )
        credential.full_clean()  # must not raise


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[TEST_ENCRYPTION_KEY])
class StrTests(TestCase):
    def test_str_does_not_contain_email_or_ciphertext(self):
        user = User.objects.create_user(username="heidi", password="pw")
        credential = EmailInboxCredential.objects.create(
            user=user,
            email_address="heidi-secret@gmail.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            app_password_encrypted="",
        )
        credential.set_app_password("abcd efgh ijkl mnop")

        rendered = str(credential)
        self.assertNotIn("heidi-secret@gmail.com", rendered)
        self.assertNotIn(credential.app_password_encrypted, rendered)
        self.assertIn("heidi", rendered)
