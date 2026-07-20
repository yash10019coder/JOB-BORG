"""Board-discovery candidate source.

Loads a maintained, open-source, MIT-licensed dataset of companies known to
use each ATS (kalil0321/ats-scrapers) instead of querying a search engine.
Superseded three earlier approaches -- see docs/plans/2026-07-18-004-feat-
job-source-discovery-plan.md Key Technical Decisions for the original
rationale (written when only Greenhouse was in scope), now stale:

- Scraping Bing's HTML search results directly.
- Rendering the same query through a full headless Chromium.
- Brave Search API -- turned out to require a paid, card-verified account
  even at our volume, not the no-strings free tier it was first taken for.

The first two get CAPTCHA-blocked from a datacenter egress IP regardless of
client fidelity (confirmed by hand); the third isn't actually free. This
dataset is a plain HTTPS GET against GitHub's raw content CDN -- a normal
file download, not a scraped session or a metered API call -- so none of
that applies. Its tradeoff: freshness depends on the upstream project's own
scrape cadence, and it only ever surfaces companies someone else has already
found, so it will always lag genuinely brand-new boards. See the plan's
Risks & Dependencies.

Kept behind the same narrow interface (``SearchResult``, ``BoardSearchClient``)
so swapping the source again is a contained change if this dataset goes
stale or the upstream project is abandoned.
"""
import csv
import io
import time
from dataclasses import dataclass, field

import requests

from apps.jobs.models import JobSource

_DATASET_BASE_URL = (
    "https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/ats-companies"
)

# Per-ats CSV filename in the upstream dataset. All four files share the
# same `name,slug,url` shape (confirmed against a live fetch of each).
_DATASET_FILENAME = {
    JobSource.ATS.GREENHOUSE: "greenhouse.csv",
    JobSource.ATS.LEVER: "lever.csv",
    JobSource.ATS.ASHBY: "ashby.csv",
    JobSource.ATS.WORKDAY: "workday.csv",
}

# Which CSV column is the board_token for this ats. Greenhouse/Lever/Ashby
# use the short `slug` column directly. Workday is the odd one out: its
# board_token is the full careers URL (see WorkdayClient), and the dataset's
# `slug` column for Workday is a `company/site` shorthand that doesn't match
# that shape -- confirmed by a live fetch of workday.csv, whose `url` column
# holds the real `https://{company}.{instance}.myworkdayjobs.com/{site}` URL.
_TOKEN_COLUMN = {
    JobSource.ATS.GREENHOUSE: "slug",
    JobSource.ATS.LEVER: "slug",
    JobSource.ATS.ASHBY: "slug",
    JobSource.ATS.WORKDAY: "url",
}

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class SearchResult:
    tokens: list = field(default_factory=list)
    pages_fetched: int = 0
    failed: bool = False


class BoardSearchClient:
    """Fetches an ATS's companies dataset and extracts board tokens.

    Never raises on a failed fetch -- signalled via ``SearchResult.failed``
    so the caller's per-run failure isolation (R6) doesn't need its own
    try/except around every call.
    """

    def __init__(
        self,
        session=None,
        *,
        max_retries=3,
        backoff_factor=0.5,
        timeout=30,
        sleep=time.sleep,
    ):
        self._session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._sleep = sleep

    def search_boards(self, ats):
        """Return a ``SearchResult`` of candidate board tokens for ``ats``.

        Raises:
            ValueError: ``ats`` has no known dataset file.
        """
        try:
            filename = _DATASET_FILENAME[ats]
        except KeyError:
            raise ValueError(
                f"No discovery dataset registered for ats={ats!r}. "
                f"Registered: {sorted(_DATASET_FILENAME)}"
            ) from None

        response = self._get_with_retry(f"{_DATASET_BASE_URL}/{filename}")
        if response is None:
            return SearchResult(tokens=[], pages_fetched=0, failed=True)

        tokens = self._extract_tokens(response.text, column=_TOKEN_COLUMN[ats])
        return SearchResult(tokens=sorted(tokens), pages_fetched=1, failed=False)

    # -- internals ---------------------------------------------------------

    def _get_with_retry(self, url):
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = self._session.get(url, timeout=self.timeout)
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
    def _extract_tokens(csv_text, *, column):
        reader = csv.DictReader(io.StringIO(csv_text))
        return {
            (row.get(column) or "").strip()
            for row in reader
            if (row.get(column) or "").strip()
        }
