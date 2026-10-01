"""Tests for the SmartRecruiters client + normalizer (SimpleTestCase)."""
import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    SmartRecruitersParseError,
    SmartRecruitersUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_smartrecruiters_job
from apps.jobs.ingestion.smartrecruiters_client import BASE_URL, SmartRecruitersClient

BOARD = "smartrecruiters"
JOBS_URL = f"{BASE_URL}/{BOARD}/postings"


def _client():
    return SmartRecruitersClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class SmartRecruitersNormalizerTests(SimpleTestCase):
    def test_normalize_valid_job(self):
        raw = {
            "id": "sr-101",
            "name": "Full Stack Software Engineer",
            "location": {"city": "San Francisco", "region": "CA", "country": "US", "remote": True},
            "jobAd": {"sections": {"jobDescription": {"text": "<p>Build apps</p>"}}},
            "ref": "https://jobs.smartrecruiters.com/sr-101",
        }
        job = normalize_smartrecruiters_job(raw)
        self.assertEqual(job["source_ats"], "smartrecruiters")
        self.assertEqual(job["source_job_id"], "sr-101")
        self.assertEqual(job["title"], "Full Stack Software Engineer")
        self.assertTrue(job["is_remote"])
        self.assertEqual(job["location_country"], "US")
        self.assertEqual(job["source_url"], "https://jobs.smartrecruiters.com/sr-101")

    def test_normalize_missing_id_raises_parse_error(self):
        with self.assertRaises(SmartRecruitersParseError):
            normalize_smartrecruiters_job({"name": "No ID"})


class SmartRecruitersClientFetchTests(SimpleTestCase):
    @responses.activate
    def test_fetch_jobs_happy_path(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            json={
                "content": [
                    {
                        "id": "sr-1",
                        "name": "Backend Engineer",
                        "location": {"city": "New York", "country": "US"},
                    }
                ],
                "totalFound": 1,
            },
            status=200,
        )
        client = _client()
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Backend Engineer")

    @responses.activate
    def test_fetch_jobs_http_500_exhausts_retries(self):
        responses.add(responses.GET, JOBS_URL, status=500)
        responses.add(responses.GET, JOBS_URL, status=500)
        responses.add(responses.GET, JOBS_URL, status=500)
        client = _client()
        with self.assertRaises(SmartRecruitersUnavailable) as ctx:
            client.fetch_jobs(BOARD)
        self.assertIsInstance(ctx.exception, IngestionUnavailable)
