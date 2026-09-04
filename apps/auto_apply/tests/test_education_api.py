"""Tests for `apps.auto_apply.greenhouse_form.education_api`.

`FakeSession` stands in for `requests.Session` -- no real HTTP call is ever
made. Each test uses a distinct board_token so the shared process-wide
LocMemCache (see config/settings/test.py) never leaks state between tests.
"""
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

    def test_empty_page_before_total_count_reached_stops_without_error(self):
        # Defensive: if the API ever returns fewer items than total_count
        # implies and then an empty page, stop rather than looping forever.
        session = FakeSession(
            {
                1: _page(["Only School"], 500),
                2: _page([], 500),
            }
        )

        result = fetch_full_list("acme-empty-page", "schools", session=session)

        self.assertEqual(result, ("Only School",))

    def test_result_is_cached_across_calls_for_same_board_and_type(self):
        session = FakeSession({1: _page(["Cached School"], 1)})

        first = fetch_full_list("acme-cache", "schools", session=session)
        second = fetch_full_list("acme-cache", "schools", session=session)

        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1, "second call should be served from cache")

    def test_duplicate_items_are_deduplicated(self):
        session = FakeSession({1: _page(["Duplicate U", "Duplicate U", "Unique U"], 3)})

        result = fetch_full_list("acme-dedup", "schools", session=session)

        self.assertEqual(result, ("Duplicate U", "Unique U"))
