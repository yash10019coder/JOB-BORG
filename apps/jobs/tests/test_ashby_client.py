"""Tests for the Ashby client + normalizer — fully DB-free (SimpleTestCase)."""
import json
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.ashby_client import BASE_URL, AshbyClient
from apps.jobs.ingestion.exceptions import (
    AshbyParseError,
    AshbyUnavailable,
    IngestionParseError,
    IngestionUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_ashby_job

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ashby_board.json"
BOARD = "acme"
JOBS_URL = f"{BASE_URL}/{BOARD}"


def _load_fixture():
    return json.loads(FIXTURE.read_text())


def _load_jobs():
    return _load_fixture()["jobs"]


def _client():
    # backoff_factor=0 + no-op sleep keeps retry tests instant.
    return AshbyClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class NormalizerTests(SimpleTestCase):
    def test_fixture_board_parses_into_normalized_dicts(self):
        jobs = [normalize_ashby_job(r) for r in _load_jobs()]
        self.assertEqual(len(jobs), 3)
        first = jobs[0]
        self.assertEqual(first["source_ats"], "ashby")
        self.assertEqual(
            first["source_job_id"], "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
        )
        self.assertEqual(first["title"], "Senior Backend Engineer")
        self.assertEqual(first["location"], "Remote - United States")
        self.assertTrue(first["is_remote"])
        self.assertEqual(first["description"], "We use Python and Kubernetes.")
        self.assertEqual(
            first["source_url"],
            "https://jobs.ashbyhq.com/acme/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
        )
        self.assertTrue(first["location_resolved"])
        self.assertEqual(first["location_country"], "US")
        self.assertEqual(first["location_alias_version"], "v1")
        self.assertIsNone(first["salary_min"])
        self.assertIsNone(first["salary_max"])

    def test_onsite_job_is_not_remote(self):
        designer = normalize_ashby_job(_load_jobs()[1])
        self.assertFalse(designer["is_remote"])
        self.assertTrue(designer["location_resolved"])
        self.assertEqual(designer["location_city"], "New York")
        self.assertEqual(designer["location_region"], "NY")
        self.assertEqual(designer["location_country"], "US")

    def test_missing_location_and_is_remote_false_normalizes_to_not_remote(self):
        analyst = normalize_ashby_job(_load_jobs()[2])
        self.assertEqual(analyst["location"], "")
        self.assertFalse(analyst["is_remote"])
        self.assertEqual(analyst["description"], "")
        self.assertFalse(analyst["location_resolved"])

    def test_job_missing_id_or_title_raises_parse_error(self):
        with self.assertRaises(AshbyParseError):
            normalize_ashby_job({"title": "No id"})
        with self.assertRaises(AshbyParseError):
            normalize_ashby_job({"id": "abc"})

    def test_non_dict_job_raises_parse_error(self):
        with self.assertRaises(AshbyParseError):
            normalize_ashby_job("not-a-dict")


class AshbyClientTests(SimpleTestCase):
    @responses.activate
    def test_fetch_returns_normalized_jobs(self):
        responses.add(responses.GET, JOBS_URL, json=_load_fixture(), status=200)
        jobs = _client().fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(
            jobs[0]["source_job_id"], "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
        )

    @responses.activate
    def test_malformed_json_raises_parse_error(self):
        responses.add(responses.GET, JOBS_URL, body="not json", status=200)
        with self.assertRaises(AshbyParseError):
            _client().fetch_jobs(BOARD)

    @responses.activate
    def test_missing_jobs_key_raises_parse_error(self):
        responses.add(responses.GET, JOBS_URL, json={"apiVersion": "1"}, status=200)
        with self.assertRaises(AshbyParseError):
            _client().fetch_jobs(BOARD)

    @responses.activate
    def test_transient_500_then_200_recovers(self):
        responses.add(responses.GET, JOBS_URL, json={"error": "boom"}, status=500)
        responses.add(responses.GET, JOBS_URL, json=_load_fixture(), status=200)
        jobs = _client().fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_exhausted_retries_raise_unavailable(self):
        for _ in range(3):  # max_retries=2 -> 3 total attempts
            responses.add(responses.GET, JOBS_URL, json={}, status=503)
        with self.assertRaises(AshbyUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_429_respects_retry_after_then_recovers(self):
        recorded = []
        client = AshbyClient(max_retries=2, backoff_factor=0, sleep=recorded.append)
        responses.add(
            responses.GET,
            JOBS_URL,
            json={},
            status=429,
            headers={"Retry-After": "2"},
        )
        responses.add(responses.GET, JOBS_URL, json=_load_fixture(), status=200)
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 3)
        # The Retry-After value drove the backoff delay.
        self.assertEqual(recorded, [2.0])

    @responses.activate
    def test_non_retryable_4xx_raises_unavailable_without_retry(self):
        # Real Ashby behavior for an unknown board: HTTP 404 with a plain
        # "Not Found" text body, not JSON (confirmed live during
        # implementation) -- the retry loop must raise before ever
        # attempting to parse this body as JSON.
        responses.add(responses.GET, JOBS_URL, body="Not Found", status=404)
        with self.assertRaises(AshbyUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_network_timeout_surfaces_as_unavailable(self):
        from requests.exceptions import ConnectTimeout

        responses.add(responses.GET, JOBS_URL, body=ConnectTimeout("timed out"))
        with self.assertRaises(AshbyUnavailable):
            _client().fetch_jobs(BOARD)


class SharedIngestionExceptionHierarchyTests(SimpleTestCase):
    def test_ashby_unavailable_is_also_ingestion_unavailable(self):
        self.assertIsInstance(AshbyUnavailable(), IngestionUnavailable)

    def test_ashby_parse_error_is_also_ingestion_parse_error(self):
        self.assertIsInstance(AshbyParseError(), IngestionParseError)
