"""Tests for the pluggable CAPTCHA-solving interface (U5).

This unit ships the interface only -- no real vendor is wired in. These
tests exercise the registry contract (registered fake solver resolves and
reports correctly; unconfigured provider resolves to None) and document,
at this module's own boundary, the "raise or exceed timeout == False"
contract that caller units (e.g. the Greenhouse browser client) rely on
and test at their own integration level.
"""
import time

from django.test import SimpleTestCase, override_settings

from apps.auto_apply.captcha.base import (
    CAPTCHA_SOLVER_REGISTRY,
    CaptchaSolver,
    ChallengeContext,
    get_solver,
)


class FakeCaptchaSolver:
    """Test double for :class:`CaptchaSolver`.

    Importable by other units' tests (e.g. U3's Greenhouse client tests) to
    inject a controllable solver without depending on a real vendor. Records
    every challenge passed to ``solve()`` for assertion, and its outcome is
    configurable via the constructor so a single class covers "always
    succeeds", "always fails", and "always raises" scenarios.
    """

    def __init__(self, result: bool = True, raises: Exception | None = None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[ChallengeContext, float]] = []

    def solve(self, challenge: ChallengeContext, timeout: float) -> bool:
        self.calls.append((challenge, timeout))
        if self.raises is not None:
            raise self.raises
        return self.result


class ChallengeContextTests(SimpleTestCase):
    def test_minimal_construction(self):
        challenge = ChallengeContext(url="https://boards.greenhouse.io/acme/jobs/123")
        self.assertEqual(challenge.url, "https://boards.greenhouse.io/acme/jobs/123")
        self.assertEqual(challenge.challenge_type, "unknown")
        self.assertEqual(challenge.metadata, {})

    def test_full_construction(self):
        challenge = ChallengeContext(
            url="https://boards.greenhouse.io/acme/jobs/123",
            challenge_type="recaptcha",
            metadata={"site_key": "abc123"},
        )
        self.assertEqual(challenge.challenge_type, "recaptcha")
        self.assertEqual(challenge.metadata, {"site_key": "abc123"})


class CaptchaSolverProtocolTests(SimpleTestCase):
    def test_fake_solver_satisfies_protocol(self):
        self.assertIsInstance(FakeCaptchaSolver(), CaptchaSolver)


class GetSolverRegistryTests(SimpleTestCase):
    def test_no_provider_configured_returns_none(self):
        """AUTO_APPLY_CAPTCHA_PROVIDER unset (default "") -> registry lookup
        returns None, meaning callers must treat every challenge as unsolved
        and fail closed (R14)."""
        with override_settings(AUTO_APPLY_CAPTCHA_PROVIDER=""):
            self.assertIsNone(get_solver())

    def test_unregistered_provider_name_returns_none(self):
        with override_settings(AUTO_APPLY_CAPTCHA_PROVIDER="some-unregistered-vendor"):
            self.assertIsNone(get_solver())

    def test_registered_fake_solver_is_returned_and_used(self):
        """Happy path: a registered fake solver's solve() returning True is
        correctly reported to the caller via the registry lookup."""
        CAPTCHA_SOLVER_REGISTRY["fake"] = FakeCaptchaSolver
        try:
            with override_settings(AUTO_APPLY_CAPTCHA_PROVIDER="fake"):
                solver = get_solver()
                self.assertIsNotNone(solver)
                challenge = ChallengeContext(url="https://boards.greenhouse.io/acme/jobs/1")
                self.assertTrue(solver.solve(challenge, timeout=5.0))
        finally:
            del CAPTCHA_SOLVER_REGISTRY["fake"]

    def test_default_registry_ships_with_no_providers(self):
        """This unit ships the interface only -- no vendor is registered by
        default, so AUTO_APPLY_CAPTCHA_PROVIDER unset always resolves to
        None regardless of environment."""
        self.assertEqual(CAPTCHA_SOLVER_REGISTRY, {})


class FakeCaptchaSolverContractTests(SimpleTestCase):
    """Documents (at this module's own boundary) the caller contract that a
    raising or timeout-exceeding solve() is treated the same as returning
    False. Full caller-side behavior (the Greenhouse client failing closed)
    is exercised by that unit's own integration tests."""

    def test_solve_returning_false(self):
        solver = FakeCaptchaSolver(result=False)
        challenge = ChallengeContext(url="https://boards.greenhouse.io/acme/jobs/1")
        self.assertFalse(solver.solve(challenge, timeout=5.0))

    def test_solve_raising_is_not_swallowed_by_the_interface(self):
        """base.py does not itself catch exceptions from solve() -- that is
        the caller's responsibility (documented contract: raising is treated
        the same as returning False by the caller, not by this interface)."""
        solver = FakeCaptchaSolver(raises=RuntimeError("vendor API down"))
        challenge = ChallengeContext(url="https://boards.greenhouse.io/acme/jobs/1")
        with self.assertRaises(RuntimeError):
            solver.solve(challenge, timeout=5.0)

    def test_solve_records_timeout_argument_for_caller_enforcement(self):
        """This interface does not enforce the timeout itself; a caller
        exceeding it (e.g. via its own wall-clock check around solve()) is
        expected to treat that the same as a False result. Here we just
        verify the timeout value is threaded through to the implementation."""
        solver = FakeCaptchaSolver(result=True)
        challenge = ChallengeContext(url="https://boards.greenhouse.io/acme/jobs/1")

        start = time.monotonic()
        solver.solve(challenge, timeout=0.01)
        elapsed = time.monotonic() - start

        self.assertEqual(solver.calls[0][1], 0.01)
        # FakeCaptchaSolver itself is instant; verifying real timeout
        # enforcement around a slow solve() belongs to the caller (U3).
        self.assertLess(elapsed, 0.01 + 1.0)
