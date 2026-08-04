"""Explicit at-rest secret encryption for stored credentials.

Wraps `cryptography.fernet.MultiFernet` behind two small functions,
`encrypt_secret()` / `decrypt_secret()`, instead of a transparent encrypted
model field. Decryption must always be an explicit, greppable call site --
never something a model field does implicitly on attribute access -- because
the first secret stored through this module (an IMAP app password) is the
highest-value credential this product will ever hold.

Key management: `settings.CREDENTIAL_ENCRYPTION_KEYS` is a list of urlsafe-
base64 Fernet keys. The first key encrypts; `MultiFernet` tries every key in
order when decrypting, so rotation is just prepending a new key and keeping
the old one(s) around until every ciphertext has been re-encrypted. There is
no default and no derivation from `SECRET_KEY` (it has an insecure dev
default, `"dev-insecure-change-me"`, in this repo) -- a forgotten env var
must fail loudly via `ImproperlyConfigured`, not silently derive a weak key.

`MultiFernet` is built lazily, inside each call, rather than cached as a
module-level singleton. A previous bug in this codebase (Celery task time
limits computed from settings at import time) broke `@override_settings` in
tests because the frozen value was read once and never revisited -- this
module deliberately avoids repeating that mistake.
"""
from django.core.checks import Warning, register
from django.core.exceptions import ImproperlyConfigured
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretDecryptionError(Exception):
    """Raised when a stored ciphertext fails to decrypt under any configured key.

    Covers both genuine tampering and an incomplete key-rotation (ciphertext
    encrypted under a key no longer present in `CREDENTIAL_ENCRYPTION_KEYS`).
    Never let a bad token silently produce garbage plaintext -- always raise.
    """


def _build_multi_fernet() -> MultiFernet:
    """Construct a `MultiFernet` from current settings.

    Deliberately NOT memoized/cached at module scope -- see the module
    docstring. Reading `settings.CREDENTIAL_ENCRYPTION_KEYS` fresh on every
    call is what makes `@override_settings` work correctly in tests.
    """
    from django.conf import settings

    keys = settings.CREDENTIAL_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured(
            "CREDENTIAL_ENCRYPTION_KEYS is empty -- set at least one Fernet "
            "key before encrypting or decrypting stored secrets. Generate "
            "one with apps.accounts.crypto.generate_key()."
        )
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt_secret(plaintext: str) -> str:
    """Encrypt `plaintext` under the first configured key.

    Returns a urlsafe-base64 Fernet token (str). Fernet tokens embed a random
    IV, so two calls with identical plaintext produce different tokens --
    this column can never be used as a DB lookup/filter key.

    Raises `ImproperlyConfigured` if no encryption keys are configured.
    """
    fernet = _build_multi_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt `token`, trying every configured key in order.

    Raises `ImproperlyConfigured` if no encryption keys are configured, and
    `SecretDecryptionError` if the token doesn't decrypt under any of them
    (tampered, corrupted, or encrypted under a key that's since been
    rotated out). Never returns anything other than the genuine plaintext.
    """
    fernet = _build_multi_fernet()
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Failed to decrypt secret under any configured "
            "CREDENTIAL_ENCRYPTION_KEYS entry."
        ) from exc


def generate_key() -> str:
    """Mint a new urlsafe-base64 Fernet key, suitable for CREDENTIAL_ENCRYPTION_KEYS.

    Documented, ops-facing way to produce a key -- run this once per new key
    and prepend the result to CREDENTIAL_ENCRYPTION_KEYS (keeping prior keys
    so existing ciphertext still decrypts until it's been re-encrypted).
    """
    return Fernet.generate_key().decode()


@register()
def check_credential_encryption_keys(app_configs, **kwargs):
    """Warn at `manage.py check` time if no encryption keys are configured in prod.

    Deliberately a warning, not an error: local/dev environments routinely
    run with DEBUG=True and no keys configured, and this check must not
    block `manage.py check` in that case.
    """
    from django.conf import settings

    if not settings.CREDENTIAL_ENCRYPTION_KEYS and not settings.DEBUG:
        return [
            Warning(
                "CREDENTIAL_ENCRYPTION_KEYS is empty with DEBUG=False. Any "
                "code path that calls encrypt_secret()/decrypt_secret() "
                "(e.g. storing an IMAP app password) will raise "
                "ImproperlyConfigured until this is set.",
                id="accounts.W001",
            )
        ]
    return []
