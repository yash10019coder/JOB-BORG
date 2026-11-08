"""Workable public Widget API client.

Pure HTTP + parsing, no database access.
Endpoint: GET https://apply.workable.com/api/v1/widget/accounts/{account_slug}?details=true
Returns a single non-paginated payload containing all job postings.
"""
import time

import requests

from .exceptions import WorkableParseError, WorkableUnavailable
from .normalizers import normalize_workable_job

BASE_URL = "https://apply.workable.com/api/v1/widget/accounts"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class WorkableClient:
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
            WorkableUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            WorkableParseError: the body is not the expected JSON shape.
        """
        url = f"{BASE_URL}/{board_token}"
        response = self._get_with_retry(url, params={"details": "true"})
        payload = self._parse_body(response)

        if isinstance(payload, dict):
            raw_jobs = payload.get("jobs")
        elif isinstance(payload, list):
            raw_jobs = payload
        else:
            raw_jobs = None

        if not isinstance(raw_jobs, list):
            raise WorkableParseError(
                f"Expected a list of jobs in response, got {type(raw_jobs).__name__}"
            )

        return [normalize_workable_job(raw) for raw in raw_jobs]

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
                    raise WorkableUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = WorkableUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise WorkableUnavailable(
            f"GET {url} failed after {self.max_retries + 1} attempts"
        ) from last_exc

    def _backoff_delay(self, attempt, response):
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
            raise WorkableParseError("Response body was not valid JSON") from exc
