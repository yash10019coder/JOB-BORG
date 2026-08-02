"""Tests for the DiscoveredBoard admin approve/reject actions."""
from pathlib import Path
from unittest import mock

import responses
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from apps.employers.models import Employer
from apps.jobs.admin import DiscoveredBoardAdmin
from apps.jobs.ingestion.greenhouse_client import BASE_URL
from apps.jobs.ingestion.lever_client import BASE_URL as LEVER_BASE_URL
from apps.jobs.models import DiscoveredBoard, JobSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_board.json"
LEVER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lever_board.json"


def _mock_board(token, status=200, job_count=None):
    if status == 200:
        body = FIXTURE.read_text()
    else:
        body = "not found"
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


class DiscoveredBoardAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = DiscoveredBoardAdmin(DiscoveredBoard, self.site)
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.post("/admin/jobs/discoveredboard/")
        request.user = get_user_model().objects.create_superuser(
            "admin", "admin@example.com", "pass"
        )
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    @responses.activate
    def test_approve_creates_job_source_and_sets_status(self):
        # Covers AE4.
        _mock_board("figma")
        board = DiscoveredBoard.objects.create(
            board_token="figma", derived_employer_name="Figma", discovered_job_count=3
        )
        qs = DiscoveredBoard.objects.filter(pk=board.pk)

        self.admin.approve(self._request(), qs)

        board.refresh_from_db()
        self.assertEqual(board.status, DiscoveredBoard.Status.APPROVED)
        self.assertIsNotNone(board.reviewed_at)
        self.assertTrue(JobSource.objects.filter(board_token="figma").exists())

    @responses.activate
    def test_approve_dispatches_by_the_boards_own_ats_not_greenhouse(self):
        # A Lever candidate must be validated via LeverClient and registered
        # with ats=LEVER, not silently treated as Greenhouse.
        _mock_lever_board("widget-co")
        board = DiscoveredBoard.objects.create(
            ats=JobSource.ATS.LEVER,
            board_token="widget-co",
            derived_employer_name="Widget Co",
            discovered_job_count=3,
        )
        qs = DiscoveredBoard.objects.filter(pk=board.pk)

        self.admin.approve(self._request(), qs)

        board.refresh_from_db()
        self.assertEqual(board.status, DiscoveredBoard.Status.APPROVED)
        job_source = JobSource.objects.get(board_token="widget-co")
        self.assertEqual(job_source.ats, JobSource.ATS.LEVER)

    def test_reject_sets_status_and_creates_no_job_source(self):
        # Covers AE4.
        board = DiscoveredBoard.objects.create(
            board_token="notion", derived_employer_name="Notion"
        )
        qs = DiscoveredBoard.objects.filter(pk=board.pk)

        self.admin.reject(self._request(), qs)

        board.refresh_from_db()
        self.assertEqual(board.status, DiscoveredBoard.Status.REJECTED)
        self.assertFalse(JobSource.objects.filter(board_token="notion").exists())

    @responses.activate
    def test_approve_only_acts_on_pending_rows_in_mixed_selection(self):
        _mock_board("pending-co")
        pending = DiscoveredBoard.objects.create(
            board_token="pending-co", derived_employer_name="Pending Co"
        )
        already_approved = DiscoveredBoard.objects.create(
            board_token="approved-co",
            derived_employer_name="Approved Co",
            status=DiscoveredBoard.Status.APPROVED,
        )
        qs = DiscoveredBoard.objects.filter(pk__in=[pending.pk, already_approved.pk])

        self.admin.approve(self._request(), qs)

        pending.refresh_from_db()
        self.assertEqual(pending.status, DiscoveredBoard.Status.APPROVED)
        self.assertTrue(JobSource.objects.filter(board_token="pending-co").exists())
        self.assertFalse(JobSource.objects.filter(board_token="approved-co").exists())

    @responses.activate
    def test_approve_leaves_row_pending_when_board_went_offline(self):
        # Error path: re-fetch at approval time fails.
        _mock_board("gone", status=404)
        board = DiscoveredBoard.objects.create(
            board_token="gone", derived_employer_name="Gone Co"
        )
        qs = DiscoveredBoard.objects.filter(pk=board.pk)

        self.admin.approve(self._request(), qs)

        board.refresh_from_db()
        self.assertEqual(board.status, DiscoveredBoard.Status.PENDING)
        self.assertIsNone(board.reviewed_at)
        self.assertFalse(JobSource.objects.filter(board_token="gone").exists())

    @responses.activate
    def test_approve_integrity_error_on_one_row_does_not_abort_the_rest_of_the_batch(self):
        # Simulates the race where a concurrent request registers the same
        # token between our check-then-create's read and write: force
        # register_job_source's "already registered?" lookup to miss (as if
        # it ran just before the concurrent create()) so its own
        # JobSource.objects.create() collides with the real unique
        # constraint and raises IntegrityError.
        _mock_board("racey-co")
        _mock_board("clean-co")
        racey = DiscoveredBoard.objects.create(
            board_token="racey-co", derived_employer_name="Racey Co"
        )
        clean = DiscoveredBoard.objects.create(
            board_token="clean-co", derived_employer_name="Clean Co"
        )
        # A JobSource for "racey-co" already exists (the concurrent winner),
        # but register_job_source's own lookup is mocked to miss it so its
        # create() call hits the real UniqueConstraint below.
        Employer.objects.create(name="Racey Co", slug="racey-co")
        existing_employer = Employer.objects.get(slug="racey-co")
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE,
            board_token="racey-co",
            employer=existing_employer,
        )
        qs = DiscoveredBoard.objects.filter(pk__in=[racey.pk, clean.pk])

        real_filter = JobSource.objects.filter

        def fake_filter(*args, **kwargs):
            if kwargs.get("board_token") == "racey-co":
                return JobSource.objects.none()
            return real_filter(*args, **kwargs)

        with mock.patch(
            "apps.jobs.ingestion.register.JobSource.objects.filter",
            side_effect=fake_filter,
        ):
            self.admin.approve(self._request(), qs)

        racey.refresh_from_db()
        clean.refresh_from_db()
        self.assertEqual(racey.status, DiscoveredBoard.Status.PENDING)
        self.assertEqual(clean.status, DiscoveredBoard.Status.APPROVED)
        self.assertTrue(JobSource.objects.filter(board_token="clean-co").exists())
        self.assertEqual(JobSource.objects.filter(board_token="racey-co").count(), 1)

    def test_similar_employer_hint_present_when_name_matches_an_existing_employer(self):
        Employer.objects.create(name="Stripe", slug="stripe")
        board = DiscoveredBoard.objects.create(
            board_token="stripe-jobs", derived_employer_name="Stripe"
        )
        self.assertIn("Stripe", self.admin.similar_employer_hint(board))

    def test_similar_employer_hint_absent_when_no_existing_employer_matches(self):
        board = DiscoveredBoard.objects.create(
            board_token="brand-new-co", derived_employer_name="Brand New Co"
        )
        self.assertEqual(self.admin.similar_employer_hint(board), "")
