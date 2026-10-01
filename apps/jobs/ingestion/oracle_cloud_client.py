"""Oracle Cloud (Fusion HCM / Oracle Recruiting Cloud) client.

Pure HTTP + parsing, no database access.
Endpoint: GET https://{domain}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
Uses finder parameters for pagination (`finder=findReqs;siteNumber={siteNumber},limit={limit},offset={offset}`).
"""
import re
import time

import requests

from .exceptions import OracleCloudParseError, OracleCloudUnavailable
from .normalizers import normalize_oracle_cloud_job

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
PAGE_LIMIT = 100
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+\.oraclecloud\.com$")


class OracleCloudClient:
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

        ``board_token`` can be formatted as ``domain#siteNumber`` (e.g.
        ``eeho.fa.us2.oraclecloud.com#CX_1``) or bare domain
        (defaults to ``CX_1``).

        Raises:
            OracleCloudUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            OracleCloudParseError: host validation failure or malformed body shape.
        """
        if "#" in board_token:
            domain, site_number = board_token.split("#", 1)
        else:
            domain, site_number = board_token, "CX_1"

        domain = domain.strip().lower()
        if not _SAFE_HOST_RE.match(domain):
            raise OracleCloudParseError(
                f"board_token domain must be a valid *.oraclecloud.com domain, got {domain!r}"
            )

        all_raw_jobs = []
        offset = 0

        headers = {
            "ora-irc-cx-userid": "00000000-0000-0000-0000-000000000000",
            "ora-irc-language": "en",
        }

        while True:
            finder_val = f"findReqs;siteNumber={site_number},limit={PAGE_LIMIT},offset={offset}"
            url = f"https://{domain}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            params = {
                "onlyData": "true",
                "finder": finder_val,
            }

            response = self._get_with_retry(url, params=params, headers=headers)
            payload = self._parse_body(response)
            if not isinstance(payload, dict):
                raise OracleCloudParseError(
                    f"Expected a dict response payload, got {type(payload).__name__}"
                )

            raw_jobs = payload.get("items")
            if not isinstance(raw_jobs, list):
                raise OracleCloudParseError(
                    f"Expected an 'items' list in response, got {type(raw_jobs).__name__}"
                )

            all_raw_jobs.extend(raw_jobs)
            total_count = payload.get("TotalJobsCount", len(all_raw_jobs))

            if len(all_raw_jobs) >= total_count or not raw_jobs:
                break
            offset += len(raw_jobs)

        return [normalize_oracle_cloud_job(raw) for raw in all_raw_jobs]

    # -- internals ---------------------------------------------------------

    def _get_with_retry(self, url, params=None, headers=None):
        last_exc = None
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = self._session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in _RETRYABLE_STATUS:
                    raise OracleCloudUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = OracleCloudUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise OracleCloudUnavailable(
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
            raise OracleCloudParseError("Response body was not valid JSON") from exc
