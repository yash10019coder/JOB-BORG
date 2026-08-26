"""Personio public XML recruiting feed client.

Pure HTTP + XML parsing, no database access.
Endpoint: GET https://{company}.jobs.personio.de/xml?language=en
Parses XML output into normalized job dicts.
"""
import time
import xml.etree.ElementTree as ET

import requests

from .exceptions import PersonioParseError, PersonioUnavailable
from .normalizers import normalize_personio_job

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PersonioClient:
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
            PersonioUnavailable: network failure, retryable status exhausted,
                or a non-retryable HTTP error.
            PersonioParseError: the body is not valid XML or missing position nodes.
        """
        if "." in board_token:
            url = f"https://{board_token}/xml"
        else:
            url = f"https://{board_token}.jobs.personio.de/xml"

        response = self._get_with_retry(url, params={"language": "en"})
        xml_tree = self._parse_xml(response.text)

        # Personio XML root typically contains <position> elements directly or under <workday>/<positions>
        positions = xml_tree.findall(".//position")
        if not positions and xml_tree.tag == "position":
            positions = [xml_tree]

        return [normalize_personio_job(pos) for pos in positions]

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
                    raise PersonioUnavailable(
                        f"GET {url} failed with HTTP {response.status_code}"
                    )
                last_exc = PersonioUnavailable(
                    f"GET {url} returned retryable HTTP {response.status_code}"
                )

            if attempt < self.max_retries:
                self._sleep(self._backoff_delay(attempt, response))

        raise PersonioUnavailable(
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
    def _parse_xml(text_content):
        try:
            return ET.fromstring(text_content)
        except ET.ParseError as exc:
            raise PersonioParseError("Response body was not valid XML") from exc
