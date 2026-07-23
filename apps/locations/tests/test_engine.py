"""Engine tests — pure, deterministic, no DB (SimpleTestCase)."""
from unittest import mock

from django.test import SimpleTestCase

from apps.locations import engine
from apps.locations.engine import LocationDataError, normalize_location

# A small dataset with a same-type city collision ("Springfield" in two
# states) and a cross-type collision ("Georgia" country vs. region), used to
# test the same-type tiebreak in isolation from whichever real dataset
# version (v1 or v2) happens to be CURRENT_LOCATION_ALIAS_VERSION -- v1.yaml
# has no city collisions to exercise this against.
_TIEBREAK_DATA = {
    "countries": [
        {"name": "US", "aliases": ["us"]},
        {"name": "Georgia", "aliases": ["georgia"]},
    ],
    "regions": [
        {
            "name": "Illinois",
            "code": "IL",
            "country": "US",
            "full_aliases": ["illinois"],
            "abbrev_aliases": ["il"],
        },
        {
            "name": "Massachusetts",
            "code": "MA",
            "country": "US",
            "full_aliases": ["massachusetts"],
            "abbrev_aliases": ["ma"],
        },
        {
            "name": "Pennsylvania",
            "code": "PA",
            "country": "US",
            "full_aliases": ["pennsylvania"],
            "abbrev_aliases": ["pa"],
        },
    ],
    "cities": [
        {
            "name": "Springfield",
            "region": "IL",
            "country": "US",
            "population": 114394,
            "feature_code": "PPLA",
            "aliases": ["springfield"],
        },
        {
            "name": "Springfield",
            "region": "MA",
            "country": "US",
            "population": 155929,
            "feature_code": "PPL",
            "aliases": ["springfield"],
        },
        {
            "name": "Springfield",
            "region": "PA",
            "country": "US",
            "population": 23363,
            "feature_code": "PPL",
            "aliases": ["springfield"],
        },
    ],
    "ambiguous_bare_tokens": ["georgia"],
}


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

    def test_bare_remote_with_nothing_else_is_no_place_info_not_unresolved(self):
        # R9: distinct from _UNRESOLVED -- there's nothing a curator could
        # add for a bare "Remote" string, so it shouldn't count as a
        # coverage gap (see apps/jobs/admin.py's location_resolved filter).
        result = normalize_location("Remote")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": True},
        )

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


class SameTypeCityTiebreakTests(SimpleTestCase):
    """Covers AE2 -- exercised against a fixture _GeoIndex, not v1/v2.yaml,
    since v1.yaml has no city-name collisions to test the tiebreak against."""

    def setUp(self):
        self.index = engine._GeoIndex(_TIEBREAK_DATA)

    def test_bare_same_type_collision_resolves_to_highest_feature_code_tier(self):
        # Illinois (PPLA, tier 1) beats Massachusetts (PPL, tier 9) even
        # though Massachusetts has a larger population.
        result = engine._resolve_bare("springfield", self.index)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "IL")

    def test_same_tier_falls_through_to_population(self):
        # Massachusetts (PPL, 155929) and Pennsylvania (PPL, 23363) share a
        # tier; population breaks the tie.
        data = {
            **_TIEBREAK_DATA,
            "cities": [c for c in _TIEBREAK_DATA["cities"] if c["region"] != "IL"],
        }
        index = engine._GeoIndex(data)
        result = engine._resolve_bare("springfield", index)
        self.assertEqual(result["region"], "MA")

    def test_comma_qualified_resolves_specific_candidate_regardless_of_tiebreak(self):
        result = engine._resolve_segments(["springfield", "ma"], self.index)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Springfield")
        self.assertEqual(result["region"], "MA")

    def test_cross_type_collision_stays_unresolved_not_tiebroken(self):
        # "georgia" is in ambiguous_bare_tokens (country vs. region
        # homograph) -- must never reach the city tiebreak logic.
        result = engine._resolve_bare("georgia", self.index)
        self.assertFalse(result["resolved"])

    def test_narrowed_segments_path_keeps_existing_partial_match_unscoped(self):
        # _resolve_segments' narrowed-candidate path is deliberately NOT
        # extended with the tiebreak (see plan Key Technical Decisions) --
        # a same-name-same-region collision it can't disambiguate stays a
        # partial match (city=None), not a guess.
        data = {
            **_TIEBREAK_DATA,
            "cities": [
                {**c, "region": "IL"} for c in _TIEBREAK_DATA["cities"]
            ],
        }
        index = engine._GeoIndex(data)
        result = engine._resolve_segments(["springfield", "illinois"], index)
        self.assertTrue(result["resolved"])
        self.assertIsNone(result["city"])
        self.assertEqual(result["region"], "IL")


class FeatureCodeTierTests(SimpleTestCase):
    def test_capital_beats_admin_seat(self):
        self.assertLess(engine.feature_code_tier("PPLC"), engine.feature_code_tier("PPLA"))

    def test_missing_code_sorts_last(self):
        self.assertGreater(engine.feature_code_tier(None), engine.feature_code_tier("PPLA5"))


class StringFormatFixesTests(SimpleTestCase):
    """Covers AE3, AE4 -- exercised against the real, currently-loaded
    dataset (v1.yaml until U4 bumps CURRENT_LOCATION_ALIAS_VERSION), so
    these use place names already curated there."""

    def test_area_suffix_stripped(self):
        result = normalize_location("Bangalore Area")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Bangalore")

    def test_country_code_prefix_stripped(self):
        # "us" is a curated v1.yaml country alias; "in" is not (v1 only
        # curates "india" as a full name, no ISO code) -- this test targets
        # whichever dataset is CURRENT_LOCATION_ALIAS_VERSION.
        result = normalize_location("US - Chicago")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Chicago")
        self.assertEqual(result["country"], "US")

    def test_world_wide_remote_is_no_place_info(self):
        result = normalize_location("World Wide - Remote")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": True},
        )

    def test_hybrid_alone_is_unresolved_not_no_place_info(self):
        # "hybrid" is not in REMOTE_MARKERS (only "remote"/"anywhere"/
        # "work from home"/"wfh"/"world wide") -- this documents current
        # scope rather than asserting a requirement; a bare "Hybrid" string
        # stays a genuine coverage gap unless a future pass adds the marker.
        result = normalize_location("Hybrid")
        self.assertFalse(result["resolved"])

    def test_suffix_and_prefix_combined(self):
        # Real-data-shaped: both fixes apply, suffix strip runs first so it
        # doesn't interfere with the prefix's start-anchored match.
        result = normalize_location("US - Seattle Area")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Seattle")

    def test_prefix_before_existing_comma_logic(self):
        result = normalize_location("US - Austin, TX")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Austin")
        self.assertEqual(result["region"], "TX")

    def test_uk_prefix_resolves_via_uk_alias_on_gb_style_country(self):
        result = normalize_location("UK - London")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "London")

    def test_prefix_with_unrecognized_place_stays_unresolved_not_raising(self):
        result = normalize_location("US - Nowhereville")
        self.assertFalse(result["resolved"])

    def test_non_country_two_letter_token_is_not_stripped(self):
        # "xx" isn't a curated country alias -- the prefix regex matches
        # syntactically but the index lookup rejects it, so the string is
        # left untouched and resolves (or not) on its own merits.
        result = normalize_location("xx - nowhereville")
        self.assertFalse(result["resolved"])
