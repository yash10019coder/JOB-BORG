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


class AshbyError(IngestionError):
    """Base class for all Ashby client failures."""


class AshbyUnavailable(AshbyError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class AshbyParseError(AshbyError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class WorkdayError(IngestionError):
    """Base class for all Workday client failures."""


class WorkdayUnavailable(WorkdayError, IngestionUnavailable):
    """The board could not be fetched (network error, retries exhausted,
    company not found, or a board_token that fails the hostname check)."""


class WorkdayParseError(WorkdayError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class SmartRecruitersError(IngestionError):
    """Base class for all SmartRecruiters client failures."""


class SmartRecruitersUnavailable(SmartRecruitersError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class SmartRecruitersParseError(SmartRecruitersError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class WorkableError(IngestionError):
    """Base class for all Workable client failures."""


class WorkableUnavailable(WorkableError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class WorkableParseError(WorkableError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class RecruiteeError(IngestionError):
    """Base class for all Recruitee client failures."""


class RecruiteeUnavailable(RecruiteeError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class RecruiteeParseError(RecruiteeError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class PersonioError(IngestionError):
    """Base class for all Personio client failures."""


class PersonioUnavailable(PersonioError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class PersonioParseError(PersonioError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""


class OracleCloudError(IngestionError):
    """Base class for all Oracle Cloud client failures."""


class OracleCloudUnavailable(OracleCloudError, IngestionUnavailable):
    """The board could not be fetched (network error, 5xx, 429, exhausted retries)."""


class OracleCloudParseError(OracleCloudError, IngestionParseError):
    """The response was reached but its body was malformed / unexpected shape."""

