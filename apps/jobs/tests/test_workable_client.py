"""Tests for the Workable client + normalizer (SimpleTestCase)."""
import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    WorkableParseError,
    WorkableUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_workable_job
from apps.jobs.ingestion.workable_client import BASE_URL, WorkableClient

BOARD = "huggingface"
JOBS_URL = f"{BASE_URL}/{BOARD}"


def _client():
    return WorkableClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class WorkableNormalizerTests(SimpleTestCase):
    def test_normalize_valid_job(self):
        raw = {
            "shortcode": "HF-101",
            "title": "Machine Learning Engineer",
            "location": {"location_str": "Remote - Paris", "city": "Paris", "telecommute": True},
            "description": "<p>ML Work</p>",
            "url": "https://apply.workable.com/huggingface/j/HF-101/",
        }
        job = normalize_workable_job(raw)
        self.assertEqual(job["source_ats"], "workable")
        self.assertEqual(job["source_job_id"], "HF-101")
        self.assertEqual(job["title"], "Machine Learning Engineer")
        self.assertTrue(job["is_remote"])

    def test_normalize_missing_title_raises_parse_error(self):
        with self.assertRaises(WorkableParseError):
            normalize_workable_job({"shortcode": "123"})


class WorkableClientFetchTests(SimpleTestCase):
    @responses.activate
    def test_fetch_jobs_happy_path(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            json={
                "jobs": [
                    {
                        "shortcode": "HF-1",
                        "title": "Research Scientist",
                        "location": {"city": "New York"},
                    }
                ]
            },
            status=200,
        )
        client = _client()
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Research Scientist")

    @responses.activate
    def test_fetch_jobs_http_404_raises_unavailable(self):
        responses.add(responses.GET, JOBS_URL, status=404)
        client = _client()
        with self.assertRaises(WorkableUnavailable) as ctx:
            client.fetch_jobs(BOARD)
        self.assertIsInstance(ctx.exception, IngestionUnavailable)
