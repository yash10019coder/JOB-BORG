"""Tests for the shared ``register_job_source`` helper."""
from pathlib import Path

import responses
from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from apps.jobs.ingestion.greenhouse_client import BASE_URL
from apps.jobs.ingestion.register import register_job_source
from apps.jobs.models import JobSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_board.json"


def _mock_board(token, status=200, body=None):
    body = body if body is not None else (FIXTURE.read_text() if status == 200 else "not found")
    responses.add(
        responses.GET, f"{BASE_URL}/{token}/jobs", body=body, status=status,
        content_type="application/json",
    )


class RegisterJobSourceTests(TestCase):
    @responses.activate
    def test_registers_employer_and_job_source(self):
        _mock_board("stripe")
        outcome = register_job_source("stripe")

        self.assertEqual(outcome.status, "registered")
        employer = Employer.objects.get(slug="stripe")
        self.assertEqual(employer.name, "Stripe")
        self.assertEqual(outcome.employer, employer)
        self.assertEqual(outcome.job_source, JobSource.objects.get(board_token="stripe"))
        self.assertGreater(outcome.job_count, 0)

    @responses.activate
    def test_employer_name_override_used(self):
        _mock_board("stripe")
        outcome = register_job_source("stripe", employer_name="Stripe Inc")
        self.assertEqual(outcome.employer.name, "Stripe Inc")

    @responses.activate
    def test_already_registered_token_returns_existing_without_duplicate(self):
        _mock_board("stripe")
        _mock_board("stripe")
        register_job_source("stripe")
        outcome = register_job_source("stripe")

        self.assertEqual(outcome.status, "already_registered")
        self.assertEqual(JobSource.objects.filter(board_token="stripe").count(), 1)

    @responses.activate
    def test_unreachable_board_propagates_greenhouse_unavailable(self):
        _mock_board("does-not-exist", status=404)
        with self.assertRaises(GreenhouseUnavailable):
            register_job_source("does-not-exist")
        self.assertFalse(Employer.objects.filter(slug="does-not-exist").exists())

    @responses.activate
    def test_malformed_response_propagates_greenhouse_parse_error(self):
        _mock_board("bad-shape", body='{"not_jobs": []}')
        with self.assertRaises(GreenhouseParseError):
            register_job_source("bad-shape")
        self.assertFalse(JobSource.objects.filter(board_token="bad-shape").exists())
