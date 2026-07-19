"""Tests for the Greenhouse-companies-dataset board-discovery client."""
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.board_search import DATASET_URL, BoardSearchClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_companies.csv"


class BoardSearchClientTests(SimpleTestCase):
    def _client(self, sleep=lambda seconds: None):
        return BoardSearchClient(sleep=sleep)

    @responses.activate
    def test_extracts_slugs_from_the_dataset(self):
        responses.add(responses.GET, DATASET_URL, body=FIXTURE.read_text(), status=200)

        result = self._client().search_greenhouse_boards()

        self.assertEqual(result.tokens, ["airbnb", "figma", "stripe"])
        self.assertFalse(result.failed)
        self.assertEqual(result.pages_fetched, 1)

    @responses.activate
    def test_rows_missing_a_slug_are_skipped(self):
        csv_text = "name,slug,url\nGood Co,goodco,https://job-boards.greenhouse.io/goodco\nBad Co,,\n"
        responses.add(responses.GET, DATASET_URL, body=csv_text, status=200)

        result = self._client().search_greenhouse_boards()

        self.assertEqual(result.tokens, ["goodco"])

    @responses.activate
    def test_non_2xx_response_is_retried_then_counted_as_a_failure_without_raising(self):
        for _ in range(4):
            responses.add(responses.GET, DATASET_URL, status=503)

        result = self._client().search_greenhouse_boards()

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])
        self.assertEqual(result.pages_fetched, 0)

    @responses.activate
    def test_non_retryable_status_fails_immediately_without_raising(self):
        responses.add(responses.GET, DATASET_URL, status=404)

        result = self._client().search_greenhouse_boards()

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])

    @responses.activate
    def test_network_error_is_retried_then_counted_as_a_failure_without_raising(self):
        import requests

        responses.add(
            responses.GET, DATASET_URL, body=requests.ConnectionError("boom")
        )
        responses.add(
            responses.GET, DATASET_URL, body=requests.ConnectionError("boom")
        )
        responses.add(
            responses.GET, DATASET_URL, body=requests.ConnectionError("boom")
        )
        responses.add(
            responses.GET, DATASET_URL, body=requests.ConnectionError("boom")
        )

        result = self._client().search_greenhouse_boards()

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])
