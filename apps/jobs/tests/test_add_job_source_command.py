"""Tests for the `add_job_source` management command."""
import json
from io import StringIO
from pathlib import Path

import responses
from django.core.management import call_command
from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.ingestion.greenhouse_client import BASE_URL
from apps.jobs.ingestion.lever_client import BASE_URL as LEVER_BASE_URL
from apps.jobs.models import JobSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_board.json"
LEVER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lever_board.json"


def _mock_board(token, status=200):
    body = FIXTURE.read_text() if status == 200 else "not found"
    responses.add(
        responses.GET, f"{BASE_URL}/{token}/jobs", body=body, status=status,
        content_type="application/json",
    )


def _mock_lever_board(token, status=200):
    body = LEVER_FIXTURE.read_text() if status == 200 else "not found"
    responses.add(
        responses.GET, f"{LEVER_BASE_URL}/{token}", body=body, status=status,
        content_type="application/json",
    )


class AddJobSourceCommandTests(TestCase):
    @responses.activate
    def test_registers_employer_and_job_source(self):
        _mock_board("stripe")
        out = StringIO()
        call_command("add_job_source", "stripe", stdout=out)

        employer = Employer.objects.get(slug="stripe")
        self.assertEqual(employer.name, "Stripe")
        source = JobSource.objects.get(board_token="stripe")
        self.assertEqual(source.employer, employer)
        self.assertIn("registered", out.getvalue())

    @responses.activate
    def test_name_override_applied(self):
        _mock_board("stripe")
        call_command("add_job_source", "stripe", "--name", "Stripe Inc.")
        # slug is derived from the employer name, not the raw board token.
        self.assertEqual(Employer.objects.get(slug="stripe-inc").name, "Stripe Inc.")

    @responses.activate
    def test_registers_multiple_tokens_in_one_call(self):
        _mock_board("stripe")
        _mock_board("airbnb")
        call_command("add_job_source", "stripe", "airbnb")
        self.assertTrue(JobSource.objects.filter(board_token="stripe").exists())
        self.assertTrue(JobSource.objects.filter(board_token="airbnb").exists())

    @responses.activate
    def test_invalid_board_token_creates_nothing(self):
        _mock_board("does-not-exist", status=404)  # non-retryable, raises immediately
        out, err = StringIO(), StringIO()
        call_command("add_job_source", "does-not-exist", stdout=out, stderr=err)
        self.assertFalse(Employer.objects.filter(slug="does-not-exist").exists())
        self.assertFalse(JobSource.objects.filter(board_token="does-not-exist").exists())

    @responses.activate
    def test_duplicate_registration_is_a_no_op(self):
        _mock_board("stripe")
        _mock_board("stripe")
        call_command("add_job_source", "stripe")
        out = StringIO()
        call_command("add_job_source", "stripe", stdout=out)
        self.assertEqual(JobSource.objects.filter(board_token="stripe").count(), 1)
        self.assertIn("already registered", out.getvalue())

    def test_name_override_rejected_for_multiple_tokens(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("add_job_source", "stripe", "airbnb", "--name", "X")

    @responses.activate
    def test_ats_flag_registers_non_greenhouse_source(self):
        _mock_lever_board("widget-co")
        call_command("add_job_source", "widget-co", "--ats", "lever")

        source = JobSource.objects.get(board_token="widget-co")
        self.assertEqual(source.ats, JobSource.ATS.LEVER)
