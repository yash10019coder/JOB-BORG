"""Tests for the Oracle Cloud client + normalizer (SimpleTestCase)."""
import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    OracleCloudParseError,
    OracleCloudUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_oracle_cloud_job
from apps.jobs.ingestion.oracle_cloud_client import OracleCloudClient

BOARD = "eeho.fa.us2.oraclecloud.com#CX_1"
JOBS_URL = "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def _client():
    return OracleCloudClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class OracleCloudNormalizerTests(SimpleTestCase):
    def test_normalize_valid_job(self):
        raw = {
            "Id": 30001,
            "Title": "Principal Cloud Architect",
            "PrimaryLocation": "Austin, TX, US",
            "ShortDescription": "Design cloud solutions",
        }
        job = normalize_oracle_cloud_job(raw)
        self.assertEqual(job["source_ats"], "oracle_cloud")
        self.assertEqual(job["source_job_id"], "30001")
        self.assertEqual(job["title"], "Principal Cloud Architect")
        self.assertEqual(job["location_country"], "US")

    def test_invalid_domain_raises_parse_error(self):
        client = _client()
        with self.assertRaises(OracleCloudParseError):
            client.fetch_jobs("malicious-host.com")


class OracleCloudClientFetchTests(SimpleTestCase):
    @responses.activate
    def test_fetch_jobs_happy_path(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            json={
                "items": [
                    {
                        "Id": 501,
                        "Title": "DevOps Engineer",
                        "PrimaryLocation": "Remote",
                    }
                ],
                "TotalJobsCount": 1,
            },
            status=200,
        )
        client = _client()
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "DevOps Engineer")
