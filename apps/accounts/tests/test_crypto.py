from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from apps.accounts.crypto import (
    SecretDecryptionError,
    check_credential_encryption_keys,
    decrypt_secret,
    encrypt_secret,
    generate_key,
)

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A])
class RoundTripTests(SimpleTestCase):
    def test_round_trip(self):
        token = encrypt_secret("hunter2")
        self.assertEqual(decrypt_secret(token), "hunter2")

    def test_encryption_is_non_deterministic(self):
        # Fernet embeds a random IV -- two encryptions of the same plaintext
        # must never produce the same ciphertext. This is why the resulting
        # column can never be a DB lookup/filter key.
        token1 = encrypt_secret("x")
        token2 = encrypt_secret("x")
        self.assertNotEqual(token1, token2)
        # Both still decrypt to the original plaintext.
        self.assertEqual(decrypt_secret(token1), "x")
        self.assertEqual(decrypt_secret(token2), "x")


class KeyRotationTests(SimpleTestCase):
    def test_rotation_and_eventual_removal(self):
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A]):
            token = encrypt_secret("rotate-me")

        # New key first, old key retained -- MultiFernet tries every key in
        # order, so ciphertext encrypted under KEY_A must still decrypt.
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_B, KEY_A]):
            self.assertEqual(decrypt_secret(token), "rotate-me")

        # Once KEY_A is fully retired, the old ciphertext can no longer be
        # read.
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_B]):
            with self.assertRaises(SecretDecryptionError):
                decrypt_secret(token)


class UnconfiguredTests(SimpleTestCase):
    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[])
    def test_encrypt_raises_when_unconfigured(self):
        with self.assertRaises(ImproperlyConfigured):
            encrypt_secret("anything")

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[])
    def test_decrypt_raises_when_unconfigured(self):
        with self.assertRaises(ImproperlyConfigured):
            decrypt_secret("anything")


@override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A])
class TamperTests(SimpleTestCase):
    def test_tampered_token_raises_typed_error_not_garbage(self):
        token = encrypt_secret("hunter2")
        # Flip one character in the middle of the token so it's still
        # syntactically token-shaped but cryptographically invalid.
        mid = len(token) // 2
        flipped_char = "A" if token[mid] != "A" else "B"
        tampered = token[:mid] + flipped_char + token[mid + 1 :]

        with self.assertRaises(SecretDecryptionError):
            decrypt_secret(tampered)


class SettingsReadPerCallTests(SimpleTestCase):
    """Regression guard: MultiFernet must NOT be cached as a module-level
    singleton. If someone naively memoized `_build_multi_fernet()`'s result
    at import/first-call time, this test would fail because the second
    `override_settings` block's key would never actually be consulted.
    """

    def test_decrypt_secret_uses_keys_current_at_call_time(self):
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A]):
            token_under_a = encrypt_secret("first-call")
            # Sanity check under the same settings block.
            self.assertEqual(decrypt_secret(token_under_a), "first-call")

        # Switch to a settings context where KEY_A is no longer present.
        # If decrypt_secret had cached a MultiFernet built from KEY_A, this
        # would incorrectly still succeed instead of raising.
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_B]):
            with self.assertRaises(SecretDecryptionError):
                decrypt_secret(token_under_a)

            # And encrypting inside this block must use KEY_B, not some
            # cached KEY_A-based Fernet -- prove it by decrypting under KEY_B
            # only, no KEY_A anywhere in the list.
            token_under_b = encrypt_secret("second-call")
            self.assertEqual(decrypt_secret(token_under_b), "second-call")

        # And switching back to KEY_A alone, the KEY_B-era token is now
        # unreadable -- again proving no stale cached Fernet survived.
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A]):
            with self.assertRaises(SecretDecryptionError):
                decrypt_secret(token_under_b)


class GenerateKeyTests(SimpleTestCase):
    def test_generate_key_is_accepted_by_fernet(self):
        key = generate_key()
        # Must not raise -- Fernet() validates key shape/length on
        # construction.
        Fernet(key.encode())

    def test_generate_key_produces_usable_key(self):
        key = generate_key()
        with override_settings(CREDENTIAL_ENCRYPTION_KEYS=[key]):
            token = encrypt_secret("via-generated-key")
            self.assertEqual(decrypt_secret(token), "via-generated-key")


class SystemCheckTests(SimpleTestCase):
    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[], DEBUG=False)
    def test_warns_when_unconfigured_and_not_debug(self):
        warnings = check_credential_encryption_keys(app_configs=None)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].id, "accounts.W001")

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[KEY_A], DEBUG=False)
    def test_no_warning_when_configured(self):
        self.assertEqual(check_credential_encryption_keys(app_configs=None), [])

    @override_settings(CREDENTIAL_ENCRYPTION_KEYS=[], DEBUG=True)
    def test_no_warning_in_debug_even_when_unconfigured(self):
        self.assertEqual(check_credential_encryption_keys(app_configs=None), [])
