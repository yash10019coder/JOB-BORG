"""Lever public Postings API client.

Pure HTTP + parsing, no database access. The live API returns a single
non-paginated JSON array per board (confirmed against
api.lever.co/v0/postings/{token}?mode=json — see the plan's execution note),
so ``fetch_jobs`` returns the full normalized list for a board in one call.
Mirrors ``greenhouse_client.py``'s retry/backoff shape exactly.
"""
import time

import requests

from .exceptions import LeverParseError, LeverUnavailable
from .normalizers import normalize_lever_job

BASE_URL = "https://api.lever.co/v0/postings"

# Status codes worth retrying (transient upstream / rate limiting).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LeverClient:
    def __init__(
        self,
        session=None,
        *,
        max_retries=3,
        backoff_factor=0.5,
        timeout=10,
        sleep=time.sleep,
    ):
        self._session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._sleep = sleep

    def fetch_jobs(self, board_token):
        """Return a list of normalized job dicts for ``board_token``.

        Raises:
            LeverUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            LeverParseError: the body is not the expected JSON shape.
        """
        url = f"{BASE_URL}/{board_token}"
        response = self._get_with_retry(url, params={"mode": "json"})
        payload = self._parse_body(response)
        if not isinstance(payload, list):
            raise LeverParseError(
                f"Expected a JSON list of postings, got {type(payload).__name__}"
            )
        return [normalize_lever_job(raw) for raw in payload]

    # -- internals ---------------------------------------------------------

    def _get_with_retry(self, url, params=None):
        last_exc = None
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = self._session.get(
                    url, params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in _RETRYABLE_STATUS:
                    raise LeverUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = LeverUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise LeverUnavailable(
            f"GET {url} failed after {self.max_retries + 1} attempts"
        ) from last_exc

    def _backoff_delay(self, attempt, response):
        # Honour a Retry-After header (seconds) on 429 when present.
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except (TypeError, ValueError):
                    pass
        return self.backoff_factor * (2 ** attempt)

    @staticmethod
    def _parse_body(response):
        try:
            return response.json()
        except ValueError as exc:
            raise LeverParseError("Response body was not valid JSON") from exc
