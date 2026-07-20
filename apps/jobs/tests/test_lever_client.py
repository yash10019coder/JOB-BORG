"""Tests for the Lever client + normalizer — fully DB-free (SimpleTestCase)."""
import json
from pathlib import Path

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    LeverParseError,
    LeverUnavailable,
)
from apps.jobs.ingestion.lever_client import BASE_URL, LeverClient
from apps.jobs.ingestion.normalizers import normalize_lever_job

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lever_board.json"
BOARD = "acme"
JOBS_URL = f"{BASE_URL}/{BOARD}"


def _load_fixture():
    return json.loads(FIXTURE.read_text())


def _client():
    # backoff_factor=0 + no-op sleep keeps retry tests instant.
    return LeverClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class NormalizerTests(SimpleTestCase):
    def test_fixture_board_parses_into_normalized_dicts(self):
        jobs = [normalize_lever_job(r) for r in _load_fixture()]
        self.assertEqual(len(jobs), 3)
        first = jobs[0]
        self.assertEqual(first["source_ats"], "lever")
        self.assertEqual(
            first["source_job_id"], "ac978161-6f46-4f6b-ad9e-a258e642751c"
        )
        self.assertEqual(first["title"], "Senior Backend Engineer")
        self.assertEqual(first["location"], "Remote - United States")
        self.assertTrue(first["is_remote"])
        self.assertEqual(first["description"], "We use Python and Kubernetes.")
        self.assertEqual(
            first["source_url"],
            "https://jobs.lever.co/acme/ac978161-6f46-4f6b-ad9e-a258e642751c",
        )
        self.assertTrue(first["location_resolved"])
        self.assertEqual(first["location_country"], "US")
        self.assertEqual(first["location_alias_version"], "v1")
        self.assertIsNone(first["salary_min"])
        self.assertIsNone(first["salary_max"])

    def test_onsite_job_is_not_remote(self):
        designer = normalize_lever_job(_load_fixture()[1])
        self.assertFalse(designer["is_remote"])
        self.assertTrue(designer["location_resolved"])
        self.assertEqual(designer["location_city"], "New York")
        self.assertEqual(designer["location_region"], "NY")
        self.assertEqual(designer["location_country"], "US")

    def test_missing_location_and_workplace_type_normalizes_to_not_remote(self):
        analyst = normalize_lever_job(_load_fixture()[2])
        self.assertEqual(analyst["location"], "")
        self.assertFalse(analyst["is_remote"])
        self.assertEqual(analyst["description"], "")
        self.assertFalse(analyst["location_resolved"])

    def test_job_missing_id_or_text_raises_parse_error(self):
        with self.assertRaises(LeverParseError):
            normalize_lever_job({"text": "No id"})
        with self.assertRaises(LeverParseError):
            normalize_lever_job({"id": "abc"})

    def test_non_dict_job_raises_parse_error(self):
        with self.assertRaises(LeverParseError):
            normalize_lever_job("not-a-dict")


class LeverClientTests(SimpleTestCase):
    @responses.activate
    def test_fetch_returns_normalized_jobs(self):
        responses.add(responses.GET, JOBS_URL, json=_load_fixture(), status=200)
        jobs = _client().fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(
            jobs[0]["source_job_id"], "ac978161-6f46-4f6b-ad9e-a258e642751c"
        )

    @responses.activate
    def test_malformed_json_raises_parse_error(self):
        responses.add(responses.GET, JOBS_URL, body="not json", status=200)
        with self.assertRaises(LeverParseError):
            _client().fetch_jobs(BOARD)

    @responses.activate
    def test_non_list_payload_raises_parse_error(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            json={"ok": False, "error": "Document not found"},
            status=200,
        )
        with self.assertRaises(LeverParseError):
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
        with self.assertRaises(LeverUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_429_respects_retry_after_then_recovers(self):
        recorded = []
        client = LeverClient(max_retries=2, backoff_factor=0, sleep=recorded.append)
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
        # Real Lever behavior for an unknown board: HTTP 404 with an
        # {"ok": false, ...} body (confirmed live during implementation).
        responses.add(
            responses.GET,
            JOBS_URL,
            json={"ok": False, "error": "Document not found"},
            status=404,
        )
        with self.assertRaises(LeverUnavailable):
            _client().fetch_jobs(BOARD)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_network_timeout_surfaces_as_unavailable(self):
        from requests.exceptions import ConnectTimeout

        responses.add(responses.GET, JOBS_URL, body=ConnectTimeout("timed out"))
        with self.assertRaises(LeverUnavailable):
            _client().fetch_jobs(BOARD)


class SharedIngestionExceptionHierarchyTests(SimpleTestCase):
    def test_lever_unavailable_is_also_ingestion_unavailable(self):
        self.assertIsInstance(LeverUnavailable(), IngestionUnavailable)

    def test_lever_parse_error_is_also_ingestion_parse_error(self):
        self.assertIsInstance(LeverParseError(), IngestionParseError)
