"""Base interfaces, data structures, and protocol for email verification providers."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class VerificationOutcome(str, Enum):
    """Structured outcomes for email code verification attempts."""

    FOUND = "found"
    NO_INBOX_CREDENTIALS = "no_inbox_credentials"
    CODE_TIMEOUT = "verification_code_timeout"
    INBOX_AUTH_FAILED = "inbox_auth_failed"
    INBOX_UNAVAILABLE = "inbox_unavailable"
    CODE_AMBIGUOUS = "verification_code_ambiguous"
    CODE_REJECTED = "verification_code_rejected"


@dataclass(frozen=True)
class CodeLookupResult:
    """Result of an email code lookup poll."""

    outcome: VerificationOutcome
    code: str | None = None

    def __repr__(self) -> str:
        code_repr = "***" if self.code else None
        return f"CodeLookupResult(outcome={self.outcome.value!r}, code={code_repr!r})"


@runtime_checkable
class EmailCodeProvider(Protocol):
    """Protocol for email code retrieval implementations."""

    def get_code(
        self, *, since: datetime, deadline_monotonic: float
    ) -> CodeLookupResult:
        """Poll for a verification email matching filters and return result."""
        ...
