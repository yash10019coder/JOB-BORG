"""Tests for the Bing board-discovery search client."""
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.board_search import SEARCH_URL, BoardSearchClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bing_search_results.html"
EMPTY_HTML = "<html><body><ol id='b_results'></ol></body></html>"


class BoardSearchClientTests(SimpleTestCase):
    def _client(self, sleep=lambda seconds: None):
        return BoardSearchClient(sleep=sleep)

    @responses.activate
    def test_extracts_and_dedupes_tokens_from_search_results(self):
        # Only mock page 0 -- subsequent pages return empty results so the
        # run doesn't loop MAX_PAGES_PER_RUN times against the same fixture.
        responses.add(responses.GET, SEARCH_URL, body=FIXTURE.read_text(), status=200)
        responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)
        responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)
        responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)
        responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)

        result = self._client().search_greenhouse_boards()

        # "stripe" appears via two different result URLs -- deduped to one.
        self.assertEqual(result.tokens, ["airbnb", "stripe"])
        self.assertFalse(result.failed)

    @responses.activate
    def test_zero_matching_results_returns_empty_token_list(self):
        for _ in range(5):
            responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)

        result = self._client().search_greenhouse_boards()

        self.assertEqual(result.tokens, [])
        self.assertFalse(result.failed)

    @responses.activate
    def test_non_2xx_response_is_retried_then_counted_as_a_failure_without_raising(self):
        # First page: exhausts retries (max_retries=3 -> 4 total attempts),
        # then falls through to page 1 rather than raising.
        for _ in range(4):
            responses.add(responses.GET, SEARCH_URL, status=503)
        responses.add(responses.GET, SEARCH_URL, body=FIXTURE.read_text(), status=200)
        for _ in range(3):
            responses.add(responses.GET, SEARCH_URL, body=EMPTY_HTML, status=200)

        result = self._client().search_greenhouse_boards()

        self.assertFalse(result.failed)
        self.assertEqual(result.tokens, ["airbnb", "stripe"])
        self.assertEqual(result.pages_fetched, 4)

    @responses.activate
    def test_failures_exceeding_threshold_stop_the_run_and_flag_failed(self):
        # FAILURE_THRESHOLD is 3 consecutive page failures; each failed page
        # exhausts its own retries (max_retries=3 -> 4 attempts/page).
        for _ in range(4 * 3):
            responses.add(responses.GET, SEARCH_URL, status=503)

        result = self._client().search_greenhouse_boards()

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])
        self.assertEqual(result.pages_fetched, 0)
