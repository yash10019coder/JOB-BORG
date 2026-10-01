"""Tests for the Recruitee client + normalizer (SimpleTestCase)."""
import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    RecruiteeParseError,
    RecruiteeUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_recruitee_job
from apps.jobs.ingestion.recruitee_client import RecruiteeClient

BOARD = "1x"
JOBS_URL = f"https://{BOARD}.recruitee.com/api/offers/"


def _client():
    return RecruiteeClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class RecruiteeNormalizerTests(SimpleTestCase):
    def test_normalize_valid_job(self):
        raw = {
            "id": 991,
            "title": "Robotics Engineer",
            "location": "Oslo, Norway",
            "remote": False,
            "description": "<p>Hardware and AI</p>",
            "careers_url": "https://1x.recruitee.com/o/robotics-engineer",
        }
        job = normalize_recruitee_job(raw)
        self.assertEqual(job["source_ats"], "recruitee")
        self.assertEqual(job["source_job_id"], "991")
        self.assertEqual(job["title"], "Robotics Engineer")
        self.assertFalse(job["is_remote"])

    def test_normalize_invalid_shape_raises_parse_error(self):
        with self.assertRaises(RecruiteeParseError):
            normalize_recruitee_job([])


class RecruiteeClientFetchTests(SimpleTestCase):
    @responses.activate
    def test_fetch_jobs_happy_path(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            json={
                "offers": [
                    {
                        "id": 100,
                        "title": "Embedded Software Developer",
                        "location": "Remote",
                        "remote": True,
                    }
                ]
            },
            status=200,
        )
        client = _client()
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Embedded Software Developer")
        self.assertTrue(jobs[0]["is_remote"])
