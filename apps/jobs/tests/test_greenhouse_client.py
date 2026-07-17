"""Tests for the Greenhouse client + normalizer — fully DB-free (SimpleTestCase)."""
import json
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from apps.jobs.ingestion.greenhouse_client import BASE_URL, GreenhouseClient
from apps.jobs.ingestion.normalizers import normalize_job

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_board.json"
BOARD = "acme"
JOBS_URL = f"{BASE_URL}/{BOARD}/jobs"


def _load_fixture():
    return json.loads(FIXTURE.read_text())


def _client():
    # backoff_factor=0 + no-op sleep keeps retry tests instant.
    return GreenhouseClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class NormalizerTests(SimpleTestCase):
    def test_fixture_board_parses_into_normalized_dicts(self):
        jobs = [normalize_job(r) for r in _load_fixture()["jobs"]]
        self.assertEqual(len(jobs), 3)
        first = jobs[0]
        self.assertEqual(first["source_ats"], "greenhouse")
        self.assertEqual(first["source_job_id"], "7982460")
        self.assertEqual(first["title"], "Senior Backend Engineer")
        self.assertEqual(first["location"], "Remote - United States")
        self.assertTrue(first["is_remote"])
        # HTML entities are unescaped for downstream keyword rules.
        self.assertIn("<strong>Python</strong>", first["description"])
        self.assertEqual(
            first["source_url"], "https://job-boards.greenhouse.io/acme/jobs/7982460"
        )

    def test_onsite_job_is_not_remote(self):
        designer = normalize_job(_load_fixture()["jobs"][1])
        self.assertFalse(designer["is_remote"])

    def test_missing_location_and_content_normalize_to_nulls(self):
        analyst = normalize_job(_load_fixture()["jobs"][2])
        self.assertEqual(analyst["location"], "")
        self.assertFalse(analyst["is_remote"])
        self.assertEqual(analyst["description"], "")

    def test_salary_always_null(self):
        job = normalize_job(_load_fixture()["jobs"][0])
        self.assertIsNone(job["salary_min"])
        self.assertIsNone(job["salary_max"])

    def test_job_missing_id_or_title_raises_parse_error(self):
        with self.assertRaises(GreenhouseParseError):
            normalize_job({"title": "No id"})
        with self.assertRaises(GreenhouseParseError):
            normalize_job({"id": 1})

    def test_non_dict_job_raises_parse_error(self):
        with self.assertRaises(GreenhouseParseError):
            normalize_job("not-a-dict")


class GreenhouseClientTests(SimpleTestCase):
    @responses.activate
    def test_fetch_returns_normalized_jobs(self):
        responses.add(responses.GET, JOBS_URL, json=_load_fixture(), status=200)
        jobs = _client().fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]["source_job_id"], "7982460")

    @responses.activate
    def test_malformed_json_raises_parse_error(self):
        responses.add(responses.GET, JOBS_URL, body="not json", status=200)
        with self.assertRaises(GreenhouseParseError):
            _client().fetch_jobs(BOARD)

    @responses.activate
    def test_missing_jobs_key_raises_parse_error(self):
        responses.add(responses.GET, JOBS_URL, json={"meta": {}}, status=200)
        with self.assertRaises(GreenhouseParseError):
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
        with self.assertRaises(GreenhouseUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_429_respects_retry_after_then_recovers(self):
        recorded = []
        client = GreenhouseClient(
            max_retries=2, backoff_factor=0, sleep=recorded.append
        )
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
        responses.add(responses.GET, JOBS_URL, json={}, status=404)
        with self.assertRaises(GreenhouseUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_network_timeout_surfaces_as_unavailable(self):
        from requests.exceptions import ConnectTimeout

        responses.add(responses.GET, JOBS_URL, body=ConnectTimeout("timed out"))
        with self.assertRaises(GreenhouseUnavailable):
            _client().fetch_jobs(BOARD)
