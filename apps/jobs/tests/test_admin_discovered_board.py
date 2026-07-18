"""Tests for the DiscoveredBoard admin approve/reject actions."""
from pathlib import Path

import responses
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from apps.employers.models import Employer
from apps.jobs.admin import DiscoveredBoardAdmin
from apps.jobs.ingestion.greenhouse_client import BASE_URL
from apps.jobs.models import DiscoveredBoard, JobSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "greenhouse_board.json"


def _mock_board(token, status=200, job_count=None):
    if status == 200:
        body = FIXTURE.read_text()
    else:
        body = "not found"
    responses.add(
        responses.GET, f"{BASE_URL}/{token}/jobs", body=body, status=status,
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
