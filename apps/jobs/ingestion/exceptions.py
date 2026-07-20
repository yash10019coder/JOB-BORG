"""Typed ingestion errors — callers never see raw HTTP client exceptions."""


class IngestionError(Exception):
    """Base class for all ATS ingestion failures, across every platform."""


class IngestionUnavailable(IngestionError):
    """A board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class IngestionParseError(IngestionError):
    """A response was reached but its body was malformed / unexpected shape."""


class GreenhouseError(IngestionError):
    """Base class for all Greenhouse client failures."""


class GreenhouseUnavailable(GreenhouseError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class GreenhouseParseError(GreenhouseError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class LeverError(IngestionError):
    """Base class for all Lever client failures."""


class LeverUnavailable(LeverError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class LeverParseError(LeverError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""
