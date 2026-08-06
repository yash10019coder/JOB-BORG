"""Typed errors for the Greenhouse browser-automation client.

Mirrors ``apps/jobs/ingestion/exceptions.py``'s shape (a single base class per
client, callers never see raw Playwright exceptions) even though this client
drives a browser instead of an HTTP session.
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.auto_apply.email_verification.base import VerificationOutcome


@dataclass
class DebugArtifacts:
    """Paths to debugging artifacts captured on a submission failure.

    Referenced, not embedded -- the caller (Celery task / admin tooling)
    decides whether/how to surface these, this module only records where
    they were written.
    """

    screenshot_path: str | None = None
    accessibility_tree_path: str | None = None


class GreenhouseFormError(Exception):
    """Base class for all Greenhouse form-automation failures."""

    def __init__(self, message: str, *, debug_artifacts: DebugArtifacts | None = None):
        super().__init__(message)
        self.debug_artifacts = debug_artifacts


class GreenhouseFormChallenged(GreenhouseFormError):
    """A bot-detection challenge (e.g. reCAPTCHA) blocked automation."""


class GreenhouseFormSchemaMismatch(GreenhouseFormError):
    """The rendered form doesn't match what this client can safely drive."""


class GreenhouseFormSubmissionFailed(GreenhouseFormError):
    """The form was filled and submitted but success could not be confirmed."""


class GreenhouseFormVerificationFailed(GreenhouseFormError):
    """Greenhouse's post-submit email-verification interstitial was detected or failed verification."""

    def __init__(
        self,
        message: str | None = None,
        *,
        outcome: "VerificationOutcome | None" = None,
        debug_artifacts: DebugArtifacts | None = None,
    ):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        if outcome is None:
            outcome = VerificationOutcome.NO_INBOX_CREDENTIALS

        self.outcome = outcome
        if message is None:
            message = f"Email verification failed: {self.outcome.value}"
        super().__init__(message, debug_artifacts=debug_artifacts)
