"""Tests for the Personio XML feed client + normalizer (SimpleTestCase)."""
import xml.etree.ElementTree as ET

import responses
from django.test import SimpleTestCase

from apps.jobs.ingestion.exceptions import (
    IngestionParseError,
    IngestionUnavailable,
    PersonioParseError,
    PersonioUnavailable,
)
from apps.jobs.ingestion.normalizers import normalize_personio_job
from apps.jobs.ingestion.personio_client import PersonioClient

BOARD = "acme"
JOBS_URL = f"https://{BOARD}.jobs.personio.de/xml"

XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<workday>
    <position>
        <id>12345</id>
        <name>Senior Accountant</name>
        <office>Berlin</office>
        <department>Finance</department>
        <jobDescriptions>
            <jobDescription>
                <name>Role Overview</name>
                <value>&lt;p&gt;Manage accounts&lt;/p&gt;</value>
            </jobDescription>
        </jobDescriptions>
    </position>
</workday>
"""


def _client():
    return PersonioClient(max_retries=2, backoff_factor=0, sleep=lambda _s: None)


class PersonioNormalizerTests(SimpleTestCase):
    def test_normalize_valid_xml_position(self):
        elem = ET.fromstring(XML_SAMPLE).find("position")
        job = normalize_personio_job(elem)
        self.assertEqual(job["source_ats"], "personio")
        self.assertEqual(job["source_job_id"], "12345")
        self.assertEqual(job["title"], "Senior Accountant")
        self.assertEqual(job["location"], "Berlin")

    def test_missing_id_raises_parse_error(self):
        elem = ET.fromstring("<position><name>No ID</name></position>")
        with self.assertRaises(PersonioParseError):
            normalize_personio_job(elem)


class PersonioClientFetchTests(SimpleTestCase):
    @responses.activate
    def test_fetch_jobs_happy_path(self):
        responses.add(
            responses.GET,
            JOBS_URL,
            body=XML_SAMPLE,
            content_type="application/xml",
            status=200,
        )
        client = _client()
        jobs = client.fetch_jobs(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Senior Accountant")
