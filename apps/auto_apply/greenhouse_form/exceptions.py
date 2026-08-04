"""Typed errors for the Greenhouse browser-automation client.

Mirrors ``apps/jobs/ingestion/exceptions.py``'s shape (a single base class per
client, callers never see raw Playwright exceptions) even though this client
drives a browser instead of an HTTP session.
"""
from dataclasses import dataclass


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
    """A bot-detection challenge (e.g. reCAPTCHA) blocked automation.

    Raised whenever a challenge is detected and either no ``CaptchaSolver``
    is configured, or the configured solver's ``solve()`` call fails,
    raises, or times out. Fail-closed by design -- this client never
    attempts to bypass a challenge by any other means.
    """


class GreenhouseFormSchemaMismatch(GreenhouseFormError):
    """The rendered form doesn't match what this client can safely drive.

    Raised both when ``inspect()`` finds a *required* field whose type
    isn't in ``field_mapping.SUPPORTED_FIELD_TYPES``, and when ``submit()``
    finds the freshly re-inspected schema no longer matches the schema a
    submission was drafted against (including option-set drift on select
    fields) -- filling with a stale field mapping is never attempted.
    """


class GreenhouseFormSubmissionFailed(GreenhouseFormError):
    """The form was filled and submitted but success could not be confirmed.

    Raised when the Submit control is clicked but the expected post-submit
    success signal never appears (rejected submission, validation error
    surfaced by the page, or an unrecognized post-submit state).
    """


class GreenhouseFormVerificationFailed(GreenhouseFormError):
    """Greenhouse's post-submit email-verification interstitial was detected.

    Some Greenhouse boards email the candidate a 6-digit code after the
    application form is submitted, which must be entered into a follow-up
    form before the application is truly accepted. As of this unit,
    ``submit()`` only *detects* this interstitial (a distinct, typed
    outcome, never confused with success or with
    ``GreenhouseFormSubmissionFailed``) -- it has not attempted any
    recovery, since no code-entry/email-polling mechanism is wired up yet.
    A later unit adds an ``outcome`` attribute here (one exception carrying
    an outcome enum, covering the full detect/poll/enter/confirm taxonomy)
    once that recovery flow exists; keep this minimal and correctly shaped
    as a peer of ``GreenhouseFormChallenged`` until then.
    """
