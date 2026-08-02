"""Tests for the ATS client dispatch registry — fully DB-free (SimpleTestCase)."""
from django.test import SimpleTestCase

from apps.jobs.ingestion.ashby_client import AshbyClient
from apps.jobs.ingestion.dispatch import get_client
from apps.jobs.ingestion.greenhouse_client import GreenhouseClient
from apps.jobs.ingestion.lever_client import LeverClient
from apps.jobs.ingestion.workday_client import WorkdayClient
from apps.jobs.models import JobSource


class DispatchTests(SimpleTestCase):
    def test_greenhouse_dispatches_to_greenhouse_client(self):
        self.assertIsInstance(get_client(JobSource.ATS.GREENHOUSE), GreenhouseClient)

    def test_lever_dispatches_to_lever_client(self):
        self.assertIsInstance(get_client(JobSource.ATS.LEVER), LeverClient)

    def test_ashby_dispatches_to_ashby_client(self):
        self.assertIsInstance(get_client(JobSource.ATS.ASHBY), AshbyClient)

    def test_workday_dispatches_to_workday_client(self):
        self.assertIsInstance(get_client(JobSource.ATS.WORKDAY), WorkdayClient)

    def test_string_ats_value_also_dispatches(self):
        # JobSource.ats is stored as a plain string on the model, so the
        # registry must key correctly off the raw string too, not just the
        # TextChoices member.
        self.assertIsInstance(get_client("greenhouse"), GreenhouseClient)

    def test_unregistered_ats_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_client("indeed")

    def test_kwargs_are_forwarded_to_the_client_constructor(self):
        client = get_client(JobSource.ATS.GREENHOUSE, max_retries=1)
        self.assertEqual(client.max_retries, 1)
