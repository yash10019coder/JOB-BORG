from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.models import DiscoveredBoard, Job, JobSource


class JobSourceModelTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme", slug="acme")

    def test_source_resolves_to_exactly_one_employer(self):
        source = JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE,
            board_token="acme",
            employer=self.employer,
        )
        self.assertEqual(source.employer, self.employer)

    def test_ats_board_token_unique(self):
        JobSource.objects.create(board_token="acme", employer=self.employer)
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobSource.objects.create(board_token="acme", employer=self.employer)


class JobModelTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme", slug="acme")

    def _make_job(self, **overrides):
        defaults = dict(
            source_ats="greenhouse",
            source_job_id="1001",
            employer=self.employer,
            title="Senior Backend Engineer",
        )
        defaults.update(overrides)
        return Job.objects.create(**defaults)

    def test_defaults(self):
        job = self._make_job()
        self.assertEqual(job.classification_tags, [])
        self.assertEqual(job.status, Job.Status.OPEN)
        self.assertTrue(job.needs_classification)
        self.assertIsNone(job.embedding)
        self.assertFalse(job.is_remote)

    def test_duplicate_source_ats_and_job_id_rejected(self):
        self._make_job(source_job_id="1001")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._make_job(source_job_id="1001")

    def test_same_source_job_id_different_ats_allowed(self):
        self._make_job(source_ats="greenhouse", source_job_id="1001")
        # A different ATS with the same external id is a distinct posting.
        other = self._make_job(source_ats="lever", source_job_id="1001")
        self.assertEqual(Job.objects.count(), 2)
        self.assertEqual(other.source_ats, "lever")

    def test_employer_deletion_is_protected_when_jobs_exist(self):
        self._make_job()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.employer.delete()

    def test_str(self):
        job = self._make_job(title="Data Scientist")
        self.assertEqual(str(job), "Data Scientist @ Acme")


class DiscoveredBoardModelTests(TestCase):
    def test_defaults(self):
        board = DiscoveredBoard.objects.create(
            board_token="stripe", derived_employer_name="Stripe"
        )
        self.assertEqual(board.status, DiscoveredBoard.Status.PENDING)
        self.assertEqual(board.ats, JobSource.ATS.GREENHOUSE)
        self.assertIsNone(board.reviewed_at)

    def test_ats_board_token_unique(self):
        DiscoveredBoard.objects.create(
            board_token="stripe", derived_employer_name="Stripe"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscoveredBoard.objects.create(
                board_token="stripe", derived_employer_name="Stripe"
            )

    def test_str(self):
        board = DiscoveredBoard.objects.create(
            board_token="stripe", derived_employer_name="Stripe"
        )
        self.assertEqual(str(board), "Greenhouse:stripe (Pending)")
