"""Vendor-agnostic CAPTCHA-solving interface.

This slice ships the interface only -- no working vendor is registered by
default. ``AUTO_APPLY_CAPTCHA_PROVIDER`` unset means :func:`get_solver`
returns ``None``, and callers (the Greenhouse browser-automation client,
built separately) are expected to treat every challenge as unsolved and
fail closed rather than proceed with an unsolved CAPTCHA.

Mirrors the registry shape used by ``apps.jobs.ingestion.dispatch``'s
``CLIENT_REGISTRY`` / ``get_client``: a small dict keyed by a settings
string, with a lookup helper that returns ``None`` (rather than raising)
when nothing is registered, since "no provider configured" is an expected,
handled state here -- not an error.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from django.conf import settings


@dataclass
class ChallengeContext:
    """Minimal, duck-typed description of a detected bot-detection challenge.

    Deliberately decoupled from Playwright: callers (e.g. the Greenhouse
    client) construct this from whatever browser-automation library they
    use, so this module has no hard dependency on any specific one.

    Attributes:
        url: The page URL where the challenge was encountered.
        challenge_type: Vendor-agnostic label for the challenge kind, e.g.
            ``"recaptcha"`` or ``"cloudflare"``. Left as a free-form string
            since this slice doesn't commit to a specific vendor's taxonomy.
        metadata: Optional extra context a solver implementation might need
            (e.g. a site key, frame reference, or screenshot path). Kept as
            an untyped mapping so this module never needs to know what a
            given vendor implementation requires.
    """

    url: str
    challenge_type: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CaptchaSolver(Protocol):
    """Protocol implemented by every CAPTCHA-solving provider."""

    def solve(self, challenge: ChallengeContext, timeout: float) -> bool:
        """Attempt to solve ``challenge``, returning whether it succeeded.

        Implementations that raise or exceed ``timeout`` are, by contract,
        treated identically to returning ``False`` by callers -- solving
        failed, and the caller fails closed. This module doesn't enforce
        the timeout itself (that's the implementation's and/or caller's
        responsibility); it just documents the contract callers rely on.
        """
        ...


# Registered CAPTCHA-solving providers, keyed by the value of
# AUTO_APPLY_CAPTCHA_PROVIDER. Empty in this unit -- no vendor is wired in
# yet, so every lookup falls through to None until a later unit registers
# one.
CAPTCHA_SOLVER_REGISTRY: dict[str, type[CaptchaSolver]] = {}


def get_solver() -> CaptchaSolver | None:
    """Return a solver instance for the configured provider, or ``None``.

    Reads ``settings.AUTO_APPLY_CAPTCHA_PROVIDER``. Returns ``None`` both
    when the setting is unset/empty and when it names a provider that
    isn't registered -- both are "no solver available" from the caller's
    point of view, and callers must fail closed in either case (R14).
    """
    provider = getattr(settings, "AUTO_APPLY_CAPTCHA_PROVIDER", "") or ""
    solver_cls = CAPTCHA_SOLVER_REGISTRY.get(provider)
    if solver_cls is None:
        return None
    return solver_cls()
