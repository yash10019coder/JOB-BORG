"""Tests for the ``discover_boards`` daily Celery task."""
from unittest import mock

from django.test import TestCase, override_settings

from apps.employers.models import Employer
from apps.jobs.ingestion.board_search import SearchResult
from apps.jobs.ingestion.exceptions import GreenhouseParseError
from apps.jobs.models import DiscoveredBoard, JobSource

_EMPTY_RESULT = SearchResult(tokens=[], pages_fetched=0, failed=False)


class _FakeSearchClient:
    """Stands in for BoardSearchClient; keyed by ats.

    Platforms not present in ``results_by_ats`` return an empty, non-failed
    result -- discover_boards now searches every registered platform each
    run, and most tests only care about exercising one of them.
    """

    def __init__(self, results_by_ats):
        self._results_by_ats = results_by_ats

    def search_boards(self, ats):
        return self._results_by_ats.get(ats, _EMPTY_RESULT)


class _FakeIngestionClient:
    """Stands in for any ATS client; behavior keyed on board token."""

    def __init__(self, by_board):
        self._by_board = by_board

    def fetch_jobs(self, board_token):
        value = self._by_board[board_token]
        if isinstance(value, Exception):
            raise value
        return value


def _run_discover_boards(search_result, by_board, ats=JobSource.ATS.GREENHOUSE):
    from apps.jobs import tasks

    with mock.patch(
        "apps.jobs.tasks.BoardSearchClient",
        return_value=_FakeSearchClient({ats: search_result}),
    ), mock.patch(
        "apps.jobs.tasks.get_client",
        return_value=_FakeIngestionClient(by_board),
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
                {
                    JobSource.ATS.GREENHOUSE: SearchResult(
                        tokens=["racey", "figma"], pages_fetched=1, failed=False
                    )
                }
            ),
        ), mock.patch(
            "apps.jobs.tasks.get_client",
            return_value=_FakeIngestionClient(
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

    def test_already_approved_or_rejected_discovered_board_is_skipped_without_a_new_validation_call(
        self,
    ):
        # R9: known_tokens excludes every DiscoveredBoard regardless of
        # status, not just PENDING -- an approved or rejected token must
        # not be re-discovered/re-validated either.
        DiscoveredBoard.objects.create(
            board_token="figma",
            derived_employer_name="Figma",
            status=DiscoveredBoard.Status.APPROVED,
        )
        DiscoveredBoard.objects.create(
            board_token="notion",
            derived_employer_name="Notion",
            status=DiscoveredBoard.Status.REJECTED,
        )

        stats = _run_discover_boards(
            SearchResult(tokens=["figma", "notion"], pages_fetched=1, failed=False),
            by_board={},  # a validation call would KeyError and fail the test
        )

        self.assertEqual(stats["already_known"], 2)
        self.assertEqual(stats["validated"], 0)
        self.assertEqual(DiscoveredBoard.objects.filter(board_token="figma").count(), 1)
        self.assertEqual(DiscoveredBoard.objects.filter(board_token="notion").count(), 1)

    def test_new_tokens_beyond_the_per_run_cap_are_skipped_and_counted(self):
        with override_settings(DISCOVERY_MAX_NEW_BOARDS_PER_RUN=2):
            stats = _run_discover_boards(
                SearchResult(
                    tokens=["figma", "notion", "airbnb", "stripe"],
                    pages_fetched=1,
                    failed=False,
                ),
                by_board={
                    "figma": [{"title": "Engineer"}],
                    "notion": [{"title": "Engineer"}],
                    # "airbnb"/"stripe" absent -- a validation call for either
                    # would KeyError and fail the test, proving the cap kept
                    # them from ever being validated.
                },
            )

        self.assertEqual(stats["found"], 4)
        self.assertEqual(stats["already_known"], 0)
        self.assertEqual(stats["validated"], 2)
        self.assertEqual(stats["skipped_for_cap"], 2)
        self.assertEqual(DiscoveredBoard.objects.count(), 2)
        self.assertTrue(DiscoveredBoard.objects.filter(board_token="figma").exists())
        self.assertTrue(DiscoveredBoard.objects.filter(board_token="notion").exists())
        self.assertFalse(DiscoveredBoard.objects.filter(board_token="airbnb").exists())
        self.assertFalse(DiscoveredBoard.objects.filter(board_token="stripe").exists())

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

    def test_each_platform_is_validated_with_its_own_client_not_a_shared_default(self):
        # The other discover_boards tests mock get_client with a fixed
        # return_value regardless of the `ats` argument, so a bug that
        # always dispatched to e.g. Greenhouse's client for every platform
        # wouldn't be caught by them. This test asserts get_client is called
        # with the matching ats for each platform searched.
        from apps.jobs import tasks

        requested_ats = []

        def fake_get_client(ats):
            requested_ats.append(ats)
            return _FakeIngestionClient(by_board={"widget-co": [{"title": "Job"}]})

        with mock.patch(
            "apps.jobs.tasks.BoardSearchClient",
            return_value=_FakeSearchClient(
                {
                    JobSource.ATS.LEVER: SearchResult(
                        tokens=["widget-co"], pages_fetched=1, failed=False
                    )
                }
            ),
        ), mock.patch(
            "apps.jobs.tasks.get_client", side_effect=fake_get_client
        ):
            tasks.discover_boards()

        # get_client is called once per platform in _DISCOVERY_ATS_PLATFORMS
        # (even those with zero candidate tokens don't call get_client, since
        # there's nothing to validate) -- Lever had one token, so it must be
        # in the requested list with the correct ats.
        self.assertIn(JobSource.ATS.LEVER, requested_ats)

    def test_lever_candidate_is_discovered_and_queued_for_review(self):
        # Covers AE2, generalized to Lever.
        stats = _run_discover_boards(
            SearchResult(tokens=["widget-co"], pages_fetched=1, failed=False),
            by_board={"widget-co": [{"title": "Engineer"}]},
            ats=JobSource.ATS.LEVER,
        )

        self.assertEqual(stats["validated"], 1)
        board = DiscoveredBoard.objects.get(board_token="widget-co")
        self.assertEqual(board.ats, JobSource.ATS.LEVER)
        self.assertEqual(board.status, DiscoveredBoard.Status.PENDING)
        # Not ingested by the hourly sweep until a reviewer approves it.
        self.assertFalse(
            JobSource.objects.filter(ats=JobSource.ATS.LEVER, board_token="widget-co").exists()
        )

    def test_ashby_candidate_is_discovered_and_queued_for_review(self):
        # Covers AE2, generalized to Ashby.
        stats = _run_discover_boards(
            SearchResult(tokens=["acme"], pages_fetched=1, failed=False),
            by_board={"acme": [{"title": "Engineer"}]},
            ats=JobSource.ATS.ASHBY,
        )

        self.assertEqual(stats["validated"], 1)
        board = DiscoveredBoard.objects.get(board_token="acme")
        self.assertEqual(board.ats, JobSource.ATS.ASHBY)
        self.assertEqual(board.status, DiscoveredBoard.Status.PENDING)

    def test_same_token_on_two_platforms_are_independent_candidates(self):
        # A Greenhouse JobSource for "acme" must not suppress discovery of an
        # unrelated Ashby "acme" -- known_tokens is scoped per ats.
        employer = Employer.objects.create(name="Acme GH", slug="acme-gh")
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="acme", employer=employer
        )
        from apps.jobs import tasks

        with mock.patch(
            "apps.jobs.tasks.BoardSearchClient",
            return_value=_FakeSearchClient(
                {
                    JobSource.ATS.GREENHOUSE: SearchResult(
                        tokens=["acme"], pages_fetched=1, failed=False
                    ),
                    JobSource.ATS.ASHBY: SearchResult(
                        tokens=["acme"], pages_fetched=1, failed=False
                    ),
                }
            ),
        ), mock.patch(
            "apps.jobs.tasks.get_client",
            return_value=_FakeIngestionClient(by_board={"acme": [{"title": "Job"}]}),
        ):
            stats = tasks.discover_boards()

        self.assertEqual(stats["already_known"], 1)  # the Greenhouse one
        self.assertEqual(stats["validated"], 1)  # the Ashby one
        self.assertTrue(
            DiscoveredBoard.objects.filter(ats=JobSource.ATS.ASHBY, board_token="acme").exists()
        )

    def test_workday_candidate_is_discovered_with_a_readable_employer_name(self):
        # Covers AE2/AE3, generalized to Workday. Workday's board_token is a
        # full URL (unlike the other platforms' short slugs) -- the derived
        # employer name must still be readable ("Acme", not the raw URL).
        stats = _run_discover_boards(
            SearchResult(
                tokens=["https://acme.wd3.myworkdayjobs.com/careers"],
                pages_fetched=1,
                failed=False,
            ),
            by_board={
                "https://acme.wd3.myworkdayjobs.com/careers": [{"title": "Engineer"}]
            },
            ats=JobSource.ATS.WORKDAY,
        )

        self.assertEqual(stats["validated"], 1)
        board = DiscoveredBoard.objects.get(
            board_token="https://acme.wd3.myworkdayjobs.com/careers"
        )
        self.assertEqual(board.ats, JobSource.ATS.WORKDAY)
        self.assertEqual(board.derived_employer_name, "Acme")

    def test_unrelated_platform_search_failure_does_not_abort_the_other_platforms(self):
        # If one platform's dataset fetch fails, the others still run.
        from apps.jobs import tasks

        with mock.patch(
            "apps.jobs.tasks.BoardSearchClient",
            return_value=_FakeSearchClient(
                {
                    JobSource.ATS.GREENHOUSE: SearchResult(
                        tokens=[], pages_fetched=0, failed=True
                    ),
                    JobSource.ATS.LEVER: SearchResult(
                        tokens=["widget-co"], pages_fetched=1, failed=False
                    ),
                }
            ),
        ), mock.patch(
            "apps.jobs.tasks.get_client",
            return_value=_FakeIngestionClient(by_board={"widget-co": [{"title": "Job"}]}),
        ):
            stats = tasks.discover_boards()

        self.assertTrue(stats["search_failed"])
        self.assertEqual(stats["validated"], 1)
        self.assertTrue(
            DiscoveredBoard.objects.filter(
                ats=JobSource.ATS.LEVER, board_token="widget-co"
            ).exists()
        )
