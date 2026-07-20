"""Tests for the ATS-companies-dataset board-discovery client."""
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.board_search import _DATASET_BASE_URL, BoardSearchClient
from apps.jobs.models import JobSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_companies.csv"

GREENHOUSE_URL = f"{_DATASET_BASE_URL}/greenhouse.csv"
LEVER_URL = f"{_DATASET_BASE_URL}/lever.csv"
ASHBY_URL = f"{_DATASET_BASE_URL}/ashby.csv"


class BoardSearchClientTests(SimpleTestCase):
    def _client(self, sleep=lambda seconds: None):
        return BoardSearchClient(sleep=sleep)

    @responses.activate
    def test_extracts_slugs_from_the_dataset(self):
        responses.add(responses.GET, GREENHOUSE_URL, body=FIXTURE.read_text(), status=200)

        result = self._client().search_boards(JobSource.ATS.GREENHOUSE)

        self.assertEqual(result.tokens, ["airbnb", "figma", "stripe"])
        self.assertFalse(result.failed)
        self.assertEqual(result.pages_fetched, 1)

    @responses.activate
    def test_lever_dataset_uses_the_lever_csv_url(self):
        csv_text = "name,slug,url\nWidget Co,widget-co,https://jobs.lever.co/widget-co\n"
        responses.add(responses.GET, LEVER_URL, body=csv_text, status=200)

        result = self._client().search_boards(JobSource.ATS.LEVER)

        self.assertEqual(result.tokens, ["widget-co"])

    @responses.activate
    def test_ashby_dataset_uses_the_ashby_csv_url(self):
        csv_text = "name,slug,url\nAcme,acme,https://jobs.ashbyhq.com/acme\n"
        responses.add(responses.GET, ASHBY_URL, body=csv_text, status=200)

        result = self._client().search_boards(JobSource.ATS.ASHBY)

        self.assertEqual(result.tokens, ["acme"])

    def test_unregistered_ats_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._client().search_boards("workday")

    @responses.activate
    def test_rows_missing_a_slug_are_skipped(self):
        csv_text = "name,slug,url\nGood Co,goodco,https://job-boards.greenhouse.io/goodco\nBad Co,,\n"
        responses.add(responses.GET, GREENHOUSE_URL, body=csv_text, status=200)

        result = self._client().search_boards(JobSource.ATS.GREENHOUSE)

        self.assertEqual(result.tokens, ["goodco"])

    @responses.activate
    def test_non_2xx_response_is_retried_then_counted_as_a_failure_without_raising(self):
        for _ in range(4):
            responses.add(responses.GET, GREENHOUSE_URL, status=503)

        result = self._client().search_boards(JobSource.ATS.GREENHOUSE)

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])
        self.assertEqual(result.pages_fetched, 0)

    @responses.activate
    def test_non_retryable_status_fails_immediately_without_raising(self):
        responses.add(responses.GET, GREENHOUSE_URL, status=404)

        result = self._client().search_boards(JobSource.ATS.GREENHOUSE)

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])

    @responses.activate
    def test_network_error_is_retried_then_counted_as_a_failure_without_raising(self):
        import requests

        for _ in range(4):
            responses.add(
                responses.GET, GREENHOUSE_URL, body=requests.ConnectionError("boom")
            )

        result = self._client().search_boards(JobSource.ATS.GREENHOUSE)

        self.assertTrue(result.failed)
        self.assertEqual(result.tokens, [])
