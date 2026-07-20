"""Tests for the Workday client + normalizer adapter.

The vendored WorkdayScraper's own pagination-cap/facet-subdivision logic is
upstream jobhive's tested responsibility, not re-verified here (mocking
httpx deeply enough to exercise that recursion would just be re-testing
vendored code). These tests cover this repo's adapter boundary: the SSRF
guard on board_token, exception translation, and the normalizer contract.
"""
from datetime import datetime, timezone
from unittest import mock

from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    WorkdayParseError,
    WorkdayUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_workday_job
from apps.jobs.ingestion.vendor.workday.exceptions import CompanyNotFoundError, ScraperError
from apps.jobs.ingestion.vendor.workday.models import Job
from apps.jobs.ingestion.workday_client import WorkdayClient

VALID_BOARD_TOKEN = "https://acme.wd3.myworkdayjobs.com/en-US/careers"


def _make_job(**overrides):
    fields = {
        "url": "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/NYC/Engineer_R-123",
        "title": "Senior Backend Engineer",
        "company": "acme",
        "ats_type": "workday",
        "ats_id": "R-123",
        "location": "Remote - United States",
        "is_remote": True,
        "description": "We use Python and Kubernetes.",
        "posted_at": None,
        "fetched_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return Job(**fields)


class NormalizerTests(SimpleTestCase):
    def test_job_normalizes_into_the_shared_dict_shape(self):
        job = _make_job()
        normalized = normalize_workday_job(job)

        self.assertEqual(normalized["source_ats"], "workday")
        self.assertEqual(normalized["source_job_id"], "R-123")
        self.assertEqual(normalized["title"], "Senior Backend Engineer")
        self.assertTrue(normalized["is_remote"])
        self.assertEqual(normalized["description"], "We use Python and Kubernetes.")
        self.assertTrue(normalized["location_resolved"])
        self.assertEqual(normalized["location_country"], "US")
        self.assertIsNone(normalized["salary_min"])
        self.assertIsNone(normalized["salary_max"])
        self.assertIn(
            "acme.wd3.myworkdayjobs.com", normalized["source_url"]
        )

    def test_onsite_job_with_explicit_is_remote_false_is_not_remote(self):
        job = _make_job(location="New York, NY", is_remote=False)
        normalized = normalize_workday_job(job)
        self.assertFalse(normalized["is_remote"])
        self.assertEqual(normalized["location_city"], "New York")

    def test_salary_passes_through_when_present(self):
        # Unlike Greenhouse/Lever/Ashby, Workday's Job model can carry real
        # salary_min/salary_max -- this must pass through unchanged, not get
        # hardcoded to None like the other three normalizers.
        job = _make_job(salary_min=120000.0, salary_max=160000.0)
        normalized = normalize_workday_job(job)
        self.assertEqual(normalized["salary_min"], 120000.0)
        self.assertEqual(normalized["salary_max"], 160000.0)

    def test_missing_location_and_description_normalize_to_empty_strings(self):
        job = _make_job(location=None, description=None, is_remote=None)
        normalized = normalize_workday_job(job)
        self.assertEqual(normalized["location"], "")
        self.assertEqual(normalized["description"], "")
        self.assertFalse(normalized["is_remote"])
        self.assertFalse(normalized["location_resolved"])


class WorkdayClientTests(SimpleTestCase):
    def test_non_workday_url_is_rejected_before_any_request(self):
        # Security: an arbitrary URL (not a *.myworkdayjobs.com host) must
        # never reach the network layer.
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            with self.assertRaises(WorkdayParseError):
                WorkdayClient().fetch_jobs("https://evil.example.com/jobs")
            scraper_cls.assert_not_called()

    def test_slash_in_company_segment_is_rejected_before_any_request(self):
        # Security (SSRF): URL_PATTERN's company group ([^.]+) matches "/"
        # too, so a naive regex-only check lets a token like
        # "https://internal-host/evil.wd3.myworkdayjobs.com/site" through --
        # the request actually built from that (company="internal-host/evil")
        # resolves to host "internal-host", not myworkdayjobs.com at all
        # (confirmed via httpx's own URL parser). The safe-label check must
        # reject this before any request is made.
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            with self.assertRaises(WorkdayParseError):
                WorkdayClient().fetch_jobs(
                    "https://internal-host/evil.wd3.myworkdayjobs.com/site"
                )
            scraper_cls.assert_not_called()

    def test_fetch_returns_normalized_jobs(self):
        fake_jobs = [_make_job(ats_id="1"), _make_job(ats_id="2")]
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            scraper_cls.return_value.fetch.return_value = fake_jobs
            jobs = WorkdayClient().fetch_jobs(VALID_BOARD_TOKEN)

        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["source_job_id"] for j in jobs}, {"1", "2"})
        scraper_cls.assert_called_once_with(
            VALID_BOARD_TOKEN,
            timeout=30.0,
            max_fetch_seconds=WorkdayClient.DEFAULT_MAX_FETCH_SECONDS,
        )

    def test_company_not_found_raises_workday_unavailable(self):
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            scraper_cls.return_value.fetch.side_effect = CompanyNotFoundError("not found")
            with self.assertRaises(WorkdayUnavailable):
                WorkdayClient().fetch_jobs(VALID_BOARD_TOKEN)

    def test_scraper_error_raises_workday_unavailable(self):
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            scraper_cls.return_value.fetch.side_effect = ScraperError("gave up after retries")
            with self.assertRaises(WorkdayUnavailable):
                WorkdayClient().fetch_jobs(VALID_BOARD_TOKEN)

    def test_malformed_response_raises_workday_parse_error(self):
        with mock.patch(
            "apps.jobs.ingestion.workday_client.WorkdayScraper"
        ) as scraper_cls:
            scraper_cls.return_value.fetch.side_effect = ValueError("not valid JSON")
            with self.assertRaises(WorkdayParseError):
                WorkdayClient().fetch_jobs(VALID_BOARD_TOKEN)


class SharedIngestionExceptionHierarchyTests(SimpleTestCase):
    def test_workday_unavailable_is_also_ingestion_unavailable(self):
        self.assertIsInstance(WorkdayUnavailable(), IngestionUnavailable)

    def test_workday_parse_error_is_also_ingestion_parse_error(self):
        self.assertIsInstance(WorkdayParseError(), IngestionParseError)
