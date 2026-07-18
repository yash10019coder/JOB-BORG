"""Tests for the ``discover_boards`` daily Celery task."""
from unittest import mock

from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.ingestion.board_search import SearchResult
from apps.jobs.ingestion.exceptions import GreenhouseParseError
from apps.jobs.models import DiscoveredBoard, JobSource


class _FakeSearchClient:
    def __init__(self, result):
        self._result = result

    def search_greenhouse_boards(self):
        return self._result


class _FakeGreenhouseClient:
    """Stands in for GreenhouseClient; behavior keyed on board token."""

    def __init__(self, by_board):
        self._by_board = by_board

    def fetch_jobs(self, board_token):
        value = self._by_board[board_token]
        if isinstance(value, Exception):
            raise value
        return value


def _run_discover_boards(search_result, by_board):
    from apps.jobs import tasks

    with mock.patch(
        "apps.jobs.tasks.BoardSearchClient",
        return_value=_FakeSearchClient(search_result),
    ), mock.patch(
        "apps.jobs.tasks.GreenhouseClient",
        return_value=_FakeGreenhouseClient(by_board),
    ):
        return tasks.discover_boards()


class DiscoverBoardsTaskTests(TestCase):
    def test_already_active_job_source_is_skipped_without_validation_call(self):
        # Covers AE1.
        employer = Employer.objects.create(name="Stripe", slug="stripe")
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="stripe", employer=employer
        )

        stats = _run_discover_boards(
            SearchResult(tokens=["stripe"], pages_fetched=1, failed=False),
            by_board={},  # empty -- a validation call would KeyError and fail the test
        )

        self.assertEqual(stats["already_known"], 1)
        self.assertEqual(stats["validated"], 0)
        self.assertEqual(DiscoveredBoard.objects.count(), 0)

    def test_new_valid_token_creates_pending_discovered_board(self):
        # Covers AE2.
        stats = _run_discover_boards(
            SearchResult(tokens=["figma"], pages_fetched=1, failed=False),
            by_board={"figma": [{"title": "Engineer"}, {"title": "Designer"}]},
        )

        self.assertEqual(stats["validated"], 1)
        board = DiscoveredBoard.objects.get(board_token="figma")
        self.assertEqual(board.status, DiscoveredBoard.Status.PENDING)
        self.assertEqual(board.derived_employer_name, "Figma")
        self.assertEqual(board.discovered_job_count, 2)

    def test_new_invalid_token_is_discarded_without_creating_a_row(self):
        # Covers AE2.
        stats = _run_discover_boards(
            SearchResult(tokens=["not-a-real-board"], pages_fetched=1, failed=False),
            by_board={"not-a-real-board": GreenhouseParseError("bad shape")},
        )

        self.assertEqual(stats["failed"], 1)
        self.assertFalse(DiscoveredBoard.objects.filter(board_token="not-a-real-board").exists())

    def test_persistence_failure_for_one_token_does_not_abort_the_rest_of_the_run(self):
        # A DiscoveredBoard already exists with the same token as one of the
        # search hits but *not* yet reflected in known_tokens -- simulates a
        # race (or any other create()-time failure) hitting the UniqueConstraint.
        # The run must still process the remaining, unrelated token.
        DiscoveredBoard.objects.create(
            board_token="racey", derived_employer_name="Racey Co"
        )
        # Bypass the known_tokens pre-filter by mutating it wouldn't be possible
        # from here, so instead assert directly against create() raising via a
        # duplicate insert triggered by two identical tokens slipping through.
        from apps.jobs import tasks

        with mock.patch(
            "apps.jobs.tasks.BoardSearchClient",
            return_value=_FakeSearchClient(
                SearchResult(tokens=["racey", "figma"], pages_fetched=1, failed=False)
            ),
        ), mock.patch(
            "apps.jobs.tasks.GreenhouseClient",
            return_value=_FakeGreenhouseClient(
                by_board={
                    "racey": [{"title": "Job"}],
                    "figma": [{"title": "Job"}],
                }
            ),
        ), mock.patch(
            "apps.jobs.tasks.DiscoveredBoard.objects.filter"
        ) as fake_filter:
            # known_tokens comes back empty so "racey" isn't pre-filtered,
            # forcing its create() to hit the real UniqueConstraint below.
            fake_filter.return_value.values_list.return_value = []
            stats = tasks.discover_boards()

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["validated"], 1)
        self.assertTrue(DiscoveredBoard.objects.filter(board_token="figma").exists())
        self.assertEqual(DiscoveredBoard.objects.filter(board_token="racey").count(), 1)

    def test_blocked_search_step_completes_the_run_and_reports_zero_found(self):
        # Covers AE3.
        stats = _run_discover_boards(
            SearchResult(tokens=[], pages_fetched=0, failed=True),
            by_board={},
        )

        self.assertEqual(stats["found"], 0)
        self.assertTrue(stats["search_failed"])
        self.assertEqual(DiscoveredBoard.objects.count(), 0)

    def test_already_pending_discovered_board_is_skipped_without_a_new_validation_call(self):
        DiscoveredBoard.objects.create(
            board_token="notion", derived_employer_name="Notion"
        )

        stats = _run_discover_boards(
            SearchResult(tokens=["notion"], pages_fetched=1, failed=False),
            by_board={},  # a validation call would KeyError and fail the test
        )

        self.assertEqual(stats["already_known"], 1)
        self.assertEqual(DiscoveredBoard.objects.filter(board_token="notion").count(), 1)

    def test_mixed_run_produces_correct_stats_and_exactly_one_new_row(self):
        employer = Employer.objects.create(name="Airbnb", slug="airbnb")
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="airbnb", employer=employer
        )

        stats = _run_discover_boards(
            SearchResult(tokens=["airbnb", "figma", "bad-token"], pages_fetched=1, failed=False),
            by_board={
                "figma": [{"title": "Engineer"}],
                "bad-token": GreenhouseParseError("bad shape"),
            },
        )

        self.assertEqual(stats["found"], 3)
        self.assertEqual(stats["already_known"], 1)
        self.assertEqual(stats["validated"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(DiscoveredBoard.objects.count(), 1)
        self.assertTrue(DiscoveredBoard.objects.filter(board_token="figma").exists())
