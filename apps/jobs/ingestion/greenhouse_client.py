"""Greenhouse public Job Board API client.

Pure HTTP + parsing, no database access. The live API returns a single
non-paginated payload per board (``{"jobs": [...], "meta": {"total": N}}``),
confirmed against boards-api.greenhouse.io — so ``fetch_jobs`` returns the full
normalized list for a board in one call.
"""
import time

import requests

from .exceptions import GreenhouseParseError, GreenhouseUnavailable
from .normalizers import normalize_job

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

# Status codes worth retrying (transient upstream / rate limiting).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GreenhouseClient:
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
            GreenhouseUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            GreenhouseParseError: the body is not the expected JSON shape.
        """
        url = f"{BASE_URL}/{board_token}/jobs"
        response = self._get_with_retry(url, params={"content": "true"})
        payload = self._parse_body(response)
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise GreenhouseParseError(
                f"Expected a 'jobs' list in the response, got {type(raw_jobs).__name__}"
            )
        return [normalize_job(raw) for raw in raw_jobs]

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
                    raise GreenhouseUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = GreenhouseUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise GreenhouseUnavailable(
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
            raise GreenhouseParseError("Response body was not valid JSON") from exc
