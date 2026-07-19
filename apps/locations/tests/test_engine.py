"""Engine tests — pure, deterministic, no DB (SimpleTestCase)."""
from unittest import mock

from django.test import SimpleTestCase

from apps.locations import engine
from apps.locations.engine import LocationDataError, normalize_location


class NormalizeLocationTests(SimpleTestCase):
    def test_full_city_region_country(self):
        result = normalize_location("New York, NY, US")
        self.assertEqual(
            result,
            {"city": "New York", "region": "NY", "country": "US", "resolved": True},
        )

    def test_city_country_only(self):
        result = normalize_location("London, UK")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "London")
        self.assertEqual(result["country"], "UK")

    def test_country_only(self):
        result = normalize_location("Germany")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": "Germany", "resolved": True},
        )

    def test_empty_string(self):
        self.assertEqual(
            normalize_location(""),
            {"city": None, "region": None, "country": None, "resolved": False},
        )

    def test_none(self):
        self.assertEqual(
            normalize_location(None),
            {"city": None, "region": None, "country": None, "resolved": False},
        )

    def test_never_raises_on_garbage(self):
        result = normalize_location("asdkfjhasldkfj")
        self.assertFalse(result["resolved"])

    def test_remote_with_country_remainder(self):
        result = normalize_location("Remote - US")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "US")
        self.assertIsNone(result["city"])

    def test_bare_remote_with_nothing_else_is_unresolved(self):
        result = normalize_location("Remote")
        self.assertFalse(result["resolved"])

    def test_bare_ambiguous_city_unresolved(self):
        result = normalize_location("Cambridge")
        self.assertFalse(result["resolved"])

    def test_bare_abbreviation_alone_unresolved(self):
        result = normalize_location("GA")
        self.assertFalse(result["resolved"])

    def test_abbreviation_resolved_via_city_context(self):
        result = normalize_location("Atlanta, GA")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "GA")
        self.assertEqual(result["country"], "US")

    def test_bare_region_country_homograph_unresolved(self):
        result = normalize_location("Georgia")
        self.assertFalse(result["resolved"])

    def test_bare_full_region_name_resolves(self):
        result = normalize_location("California")
        self.assertEqual(
            result,
            {"city": None, "region": "CA", "country": "US", "resolved": True},
        )

    def test_unicode_and_unaccented_forms_match_same_city(self):
        accented = normalize_location("München")
        unaccented = normalize_location("Munich")
        self.assertTrue(accented["resolved"])
        self.assertEqual(accented, unaccented)

    def test_multi_location_parses_first_segment_only(self):
        result = normalize_location("New York, NY or Remote")
        self.assertEqual(
            result,
            {"city": "New York", "region": "NY", "country": "US", "resolved": True},
        )

    def test_mixed_case_whitespace_and_punctuation(self):
        result = normalize_location("  new york,  ny.  ")
        self.assertEqual(
            result,
            {"city": "New York", "region": "NY", "country": "US", "resolved": True},
        )

    def test_deterministic(self):
        self.assertEqual(
            normalize_location("Austin, TX, US"),
            normalize_location("Austin, TX, US"),
        )

    def test_unrecognized_tail_does_not_fall_back_to_unconstrained_city(self):
        # Adversarial-review regression: an unrecognized tail segment must
        # not be silently discarded in favor of a confident head-only city
        # match -- that reintroduces a "confidently wrong" version of the
        # exact bug class this dataset exists to prevent.
        result = normalize_location("Austin, Georgia")
        self.assertFalse(result["resolved"])

    def test_unrecognized_tail_stays_unresolved_for_other_cities_too(self):
        for raw in ("Chicago, Timbuktu", "Seattle, Antarctica"):
            with self.subTest(raw=raw):
                self.assertFalse(normalize_location(raw)["resolved"])


class LoadIndexTests(SimpleTestCase):
    def test_missing_version_raises_location_data_error(self):
        with self.assertRaises(LocationDataError):
            engine._load_index("does-not-exist")


class NormalizeLocationNeverRaisesTests(SimpleTestCase):
    def test_missing_dataset_file_does_not_raise(self):
        with mock.patch.object(engine, "_load_index", side_effect=LocationDataError("missing")):
            result = normalize_location("New York, NY, US")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": False},
        )

    def test_non_string_input_does_not_raise(self):
        for bad in (12345, ["New York"], {"city": "New York"}):
            with self.subTest(bad=bad):
                result = normalize_location(bad)
                self.assertFalse(result["resolved"])
