"""Typed ingestion errors — callers never see raw HTTP client exceptions."""


class IngestionError(Exception):
    """Base class for all ATS ingestion failures, across every platform."""


class IngestionUnavailable(IngestionError):
    """A board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class PermanentBoardUnavailable(IngestionUnavailable):
    """A board is permanently unavailable until its registration is repaired."""

    is_permanent = True


class IngestionParseError(IngestionError):
    """A response was reached but its body was malformed / unexpected shape."""


class GreenhouseError(IngestionError):
    """Base class for all Greenhouse client failures."""


class GreenhouseUnavailable(GreenhouseError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class GreenhouseBoardNotFound(GreenhouseUnavailable, PermanentBoardUnavailable):
    """Greenhouse returned 404 for this board token."""


class GreenhouseParseError(GreenhouseError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class LeverError(IngestionError):
    """Base class for all Lever client failures."""


class LeverUnavailable(LeverError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class LeverBoardNotFound(LeverUnavailable, PermanentBoardUnavailable):
    """Lever returned 404 for this board token."""


class LeverParseError(LeverError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class AshbyError(IngestionError):
    """Base class for all Ashby client failures."""


class AshbyUnavailable(AshbyError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class AshbyBoardNotFound(AshbyUnavailable, PermanentBoardUnavailable):
    """Ashby returned 404 for this board token."""


class AshbyParseError(AshbyError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class WorkdayError(IngestionError):
    """Base class for all Workday client failures."""


class WorkdayUnavailable(WorkdayError, IngestionUnavailable):
    """The board could not be fetched (network error, retries exhausted,
    company not found, or a board_token that fails the hostname check)."""


class WorkdayParseError(WorkdayError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""
