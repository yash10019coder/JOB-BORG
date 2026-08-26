"""SmartRecruiters public Posting API client.

Pure HTTP + parsing, no database access.
Endpoint: GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings
Handles offset-based pagination via `limit`, `offset`, and `totalFound`.
"""
import time

import requests

from .exceptions import SmartRecruitersParseError, SmartRecruitersUnavailable
from .normalizers import normalize_smartrecruiters_job

BASE_URL = "https://api.smartrecruiters.com/v1/companies"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
PAGE_LIMIT = 100


class SmartRecruitersClient:
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
            SmartRecruitersUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            SmartRecruitersParseError: the body is not the expected JSON shape.
        """
        all_raw_jobs = []
        offset = 0

        while True:
            url = f"{BASE_URL}/{board_token}/postings"
            response = self._get_with_retry(
                url, params={"limit": PAGE_LIMIT, "offset": offset}
            )
            payload = self._parse_body(response)
            if not isinstance(payload, dict):
                raise SmartRecruitersParseError(
                    f"Expected a dict response payload, got {type(payload).__name__}"
                )

            raw_jobs = payload.get("content")
            if not isinstance(raw_jobs, list):
                raise SmartRecruitersParseError(
                    f"Expected a 'content' list in response, got {type(raw_jobs).__name__}"
                )

            all_raw_jobs.extend(raw_jobs)
            total_found = payload.get("totalFound", 0)

            if len(all_raw_jobs) >= total_found or not raw_jobs:
                break
            offset += len(raw_jobs)

        return [normalize_smartrecruiters_job(raw) for raw in all_raw_jobs]

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
                    raise SmartRecruitersUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = SmartRecruitersUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise SmartRecruitersUnavailable(
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
            raise SmartRecruitersParseError("Response body was not valid JSON") from exc
