"""Tests for `apps.auto_apply.greenhouse_form.education_api`.

`FakeSession` stands in for `requests.Session` -- no real HTTP call is ever
made. Each test uses a distinct board_token so the shared process-wide
LocMemCache (see config/settings/test.py) never leaks state between tests.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.auto_apply.greenhouse_form.education_api import (
    education_type_for_control_id,
    fetch_full_list,
)


class EducationTypeForControlIdTests(SimpleTestCase):
    def test_school_control_id_maps_to_schools(self):
        self.assertEqual(education_type_for_control_id("school--0"), "schools")

    def test_degree_control_id_maps_to_degrees(self):
        self.assertEqual(education_type_for_control_id("degree--1"), "degrees")

    def test_discipline_control_id_maps_to_disciplines(self):
        self.assertEqual(education_type_for_control_id("discipline--0"), "disciplines")

    def test_unrelated_control_id_returns_none(self):
        self.assertIsNone(education_type_for_control_id("country"))
        self.assertIsNone(education_type_for_control_id("first_name"))

    def test_blank_control_id_returns_none(self):
        self.assertIsNone(education_type_for_control_id(""))


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body


class FakeSession:
    """Records every GET and returns a pre-programmed response per page."""

    def __init__(self, responses_by_page=None, raises=None):
        self.responses_by_page = responses_by_page or {}
        self.raises = raises
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        page = params["page"]
        return self.responses_by_page[page]


def _page(items, total_count):
    return FakeResponse(
        200,
        {"items": [{"id": i, "text": t} for i, t in enumerate(items)], "meta": {"total_count": total_count}},
    )


class FetchFullListTests(SimpleTestCase):
    def test_single_page_returns_all_items(self):
        session = FakeSession({1: _page(["Bachelor's Degree", "Master's Degree"], 2)})

        result = fetch_full_list("acme-single", "degrees", session=session)

        self.assertEqual(result, ("Bachelor's Degree", "Master's Degree"))
        self.assertEqual(len(session.calls), 1)

    def test_multi_page_paginates_until_total_count_reached(self):
        session = FakeSession(
            {
                1: _page([f"School {i}" for i in range(100)], 250),
                2: _page([f"School {i}" for i in range(100, 200)], 250),
                3: _page([f"School {i}" for i in range(200, 250)], 250),
            }
        )

        result = fetch_full_list("acme-multi", "schools", session=session)

        self.assertEqual(len(result), 250)
        self.assertEqual(result[0], "School 0")
        self.assertEqual(result[-1], "School 249")
        self.assertEqual(len(session.calls), 3)

    def test_non_200_status_returns_none(self):
        session = FakeSession({1: FakeResponse(404, {})})

        result = fetch_full_list("acme-404", "schools", session=session)

        self.assertIsNone(result)

    def test_unexpected_response_shape_returns_none(self):
        session = FakeSession({1: FakeResponse(200, {"unexpected": "shape"})})

        result = fetch_full_list("acme-badshape", "schools", session=session)

        self.assertIsNone(result)

    def test_network_error_returns_none(self):
        session = FakeSession(raises=ConnectionError("boom"))

        result = fetch_full_list("acme-network-error", "schools", session=session)

        self.assertIsNone(result)

    def test_empty_page_before_total_count_reached_returns_none_and_is_not_cached(self):
        # If the API returns fewer items than total_count implies and then an
        # empty page, stop rather than looping forever -- and do NOT hand back
        # the truncated list as if it were complete.
        session = FakeSession(
            {
                1: _page(["Only School"], 500),
                2: _page([], 500),
            }
        )

        result = fetch_full_list("acme-empty-page", "schools", session=session)

        self.assertIsNone(result)
        self.assertIsNone(cache.get("greenhouse_education_api:acme-empty-page:schools"))
        session.responses_by_page = {1: _page(["Complete School"], 1)}
        self.assertEqual(
            fetch_full_list("acme-empty-page", "schools", session=session),
            ("Complete School",),
        )

    def test_invalid_total_counts_return_none_without_caching(self):
        for index, count in enumerate((-1, True, "1", 1.5)):
            with self.subTest(total_count=count):
                board = f"acme-invalid-count-{index}"
                cache_key = f"greenhouse_education_api:{board}:schools"
                cache.delete(cache_key)
                session = FakeSession({1: _page(["Only School"], count)})

                self.assertIsNone(fetch_full_list(board, "schools", session=session))
                self.assertIsNone(cache.get(cache_key))
                self.assertEqual(len(session.calls), 1)

    def test_missing_total_count_is_not_cached(self):
        session = FakeSession({1: _page(["Sample"], None)})
        self.assertIsNone(fetch_full_list("acme-no-total", "schools", session=session))
        session.responses_by_page = {1: _page(["Full list"], 1)}
        self.assertEqual(fetch_full_list("acme-no-total", "schools", session=session), ("Full list",))

    @patch("apps.auto_apply.greenhouse_form.education_api._MAX_PAGES", 1)
    def test_page_limit_does_not_cache_partial_list(self):
        session = FakeSession({1: _page(["Sample"], 2)})
        self.assertIsNone(fetch_full_list("acme-page-limit", "schools", session=session))
        session.responses_by_page = {1: _page(["Full list"], 1)}
        self.assertEqual(fetch_full_list("acme-page-limit", "schools", session=session), ("Full list",))

    def test_result_is_cached_across_calls_for_same_board_and_type(self):
        session = FakeSession({1: _page(["Cached School"], 1)})

        first = fetch_full_list("acme-cache", "schools", session=session)
        second = fetch_full_list("acme-cache", "schools", session=session)

        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1, "second call should be served from cache")

    def test_legacy_partial_cache_is_not_reused(self):
        cache.set("greenhouse_education_api:acme-legacy:schools", ("Partial",))
        self.addCleanup(cache.delete, "greenhouse_education_api:acme-legacy:schools")
        session = FakeSession({1: _page(["Complete"], 1)})
        self.assertEqual(fetch_full_list("acme-legacy", "schools", session=session), ("Complete",))
        self.assertEqual(len(session.calls), 1)

    def test_empty_page_with_invalid_total_count_returns_none(self):
        session = FakeSession({1: _page([], "invalid")})
        self.assertIsNone(fetch_full_list("acme-invalid-total", "schools", session=session))

    def test_duplicate_items_are_deduplicated(self):
        session = FakeSession({1: _page(["Duplicate U", "Duplicate U", "Unique U"], 3)})

        result = fetch_full_list("acme-dedup", "schools", session=session)

        self.assertEqual(result, ("Duplicate U", "Unique U"))
