"""Typed ingestion errors — callers never see raw HTTP client exceptions."""


class GreenhouseError(Exception):
    """Base class for all Greenhouse client failures."""


class GreenhouseUnavailable(GreenhouseError):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class GreenhouseParseError(GreenhouseError):
    """The response was reached but its body was malformed / unexpected shape."""
