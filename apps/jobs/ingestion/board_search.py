"""Board-discovery search client.

Queries Bing (not Google — see docs/plans/2026-07-18-004-feat-job-source-
discovery-plan.md Key Technical Decisions for why) for public
boards.greenhouse.io URLs and extracts candidate board tokens.

Kept behind a narrow interface so swapping the search source later (a paid
search API, a directory dataset) is a contained change if scraping proves
unstable in practice — see the plan's Risks & Dependencies.
"""
import re
import time
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://www.bing.com/search"
QUERY = "site:boards.greenhouse.io"

# Capped, not open-ended pagination -- bounds both block risk and query
# volume. The exact ceiling is a Deferred to Implementation item in the
# plan; this is a conservative starting point.
MAX_PAGES_PER_RUN = 5
RESULTS_PER_PAGE = 10
FAILURE_THRESHOLD = 3

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# A realistic non-default User-Agent -- the bare `requests` default UA is
# one of the first signals search engines filter on.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TOKEN_PATTERN = re.compile(r"boards\.greenhouse\.io/([a-zA-Z0-9][a-zA-Z0-9-]*)")


@dataclass
class SearchResult:
    tokens: list = field(default_factory=list)
    pages_fetched: int = 0
    failed: bool = False


class BoardSearchClient:
    """Searches Bing for Greenhouse board URLs.

    Never raises on a failed query -- a bad run is signalled through
    ``SearchResult.failed`` so the caller's per-run failure isolation (R6)
    doesn't need its own try/except around every call.
    """

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

    def search_greenhouse_boards(self):
        tokens = set()
        pages_fetched = 0
        consecutive_failures = 0

        for page in range(MAX_PAGES_PER_RUN):
            response = self._get_with_retry(page)
            if response is None:
                consecutive_failures += 1
                if consecutive_failures >= FAILURE_THRESHOLD:
                    return SearchResult(
                        tokens=sorted(tokens), pages_fetched=pages_fetched, failed=True
                    )
                continue

            consecutive_failures = 0
            pages_fetched += 1
            tokens.update(self._extract_tokens(response.text))

        return SearchResult(tokens=sorted(tokens), pages_fetched=pages_fetched, failed=False)

    # -- internals ---------------------------------------------------------

    def _get_with_retry(self, page):
        params = {"q": QUERY, "first": page * RESULTS_PER_PAGE + 1}
        headers = {"User-Agent": _USER_AGENT}

        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = self._session.get(
                    SEARCH_URL, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException:
                response = None
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in _RETRYABLE_STATUS:
                    return None

            if attempt < self.max_retries:
                self._sleep(self.backoff_factor * (2**attempt))

        return None

    @staticmethod
    def _extract_tokens(html):
        return {match.group(1) for match in _TOKEN_PATTERN.finditer(html)}
