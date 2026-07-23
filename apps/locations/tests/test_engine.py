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
        {"name": "US", "aliases": ["us"], "population": 327167434},
        {"name": "Georgia", "aliases": ["georgia"], "population": 3720400},
        {"name": "Luxembourg", "aliases": ["lu"], "population": 602005},
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
        {
            "name": "California",
            "code": "CA",
            "country": "US",
            "full_aliases": ["california"],
            "abbrev_aliases": ["ca"],
        },
        {
            "name": "Capellen",
            "code": "CA",
            "country": "Luxembourg",
            "full_aliases": ["capellen"],
            "abbrev_aliases": ["ca"],
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
        # "New York City" is GeoNames' canonical name (v1.yaml hand-curated
        # the shorter "New York" instead) -- same real city, different
        # display string, which is exactly the class of change U4's
        # dry-run diff exists to surface before a real cutover.
        result = normalize_location("New York, NY, US")
        self.assertEqual(
            result,
            {"city": "New York City", "region": "NY", "country": "US", "resolved": True},
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

    def test_bare_same_type_city_collision_now_resolves_via_tiebreak(self):
        # Deliberate behavior change (see plan Key Technical Decisions):
        # v1.yaml hand-forced "cambridge" into ambiguous_bare_tokens because
        # it had no tiebreak mechanism. v2's generation only marks CROSS-type
        # collisions ambiguous (see geodata_generation.py) -- a same-type
        # collision like Cambridge, UK vs. Cambridge, MA is exactly what
        # U2's population/feature-code tiebreak exists to resolve instead of
        # blanket-unresolving.
        result = normalize_location("Cambridge")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Cambridge")

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
            {"city": "New York City", "region": "NY", "country": "US", "resolved": True},
        )

    def test_mixed_case_whitespace_and_punctuation(self):
        result = normalize_location("  new york,  ny.  ")
        self.assertEqual(
            result,
            {"city": "New York City", "region": "NY", "country": "US", "resolved": True},
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
        # "Timbuktu" isn't a real country/region alias under either dataset
        # version -- unlike "Antarctica" (a real, GeoNames-recognized
        # country under v2's worldwide coverage; v1 simply didn't curate
        # it), so it stays a genuinely unrecognized tail rather than a
        # dataset-coverage artifact.
        result = normalize_location("Chicago, Timbuktu")
        self.assertFalse(result["resolved"])

    def test_recognized_tail_with_no_matching_city_is_a_partial_match(self):
        # "Antarctica" is a real GeoNames country (v1 never curated it, so
        # this string was unresolved there) -- the tail resolves correctly,
        # but no Seattle exists in Antarctica, so the existing "no confident
        # partial match" invariant still holds: city stays None rather than
        # falling back to the US Seattle.
        result = normalize_location("Seattle, Antarctica")
        self.assertTrue(result["resolved"])
        self.assertIsNone(result["city"])
        self.assertEqual(result["country"], "AQ")


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


class CommaContextFullAliasTests(SimpleTestCase):
    """A region-vs-city same-name collision (e.g. "Washington" the state vs.
    Washington, D.C.) demotes the region's bare claim so the city wins there
    -- but comma-context ("Seattle, Washington") has no such collision and
    must keep working via `comma_context_full_aliases`."""

    def setUp(self):
        data = {
            "countries": [{"name": "US", "aliases": ["us"], "population": 327167434}],
            "regions": [
                {
                    "name": "Washington",
                    "code": "WA",
                    "country": "US",
                    "full_aliases": [],
                    "comma_context_full_aliases": ["washington"],
                    "abbrev_aliases": ["wa"],
                }
            ],
            "cities": [
                {
                    "name": "Washington",
                    "region": "DC",
                    "country": "US",
                    "population": 689545,
                    "feature_code": "PPLC",
                    "aliases": ["washington"],
                },
                {
                    "name": "Seattle",
                    "region": "WA",
                    "country": "US",
                    "population": 737015,
                    "feature_code": "PPLA2",
                    "aliases": ["seattle"],
                },
            ],
            "ambiguous_bare_tokens": [],
        }
        self.index = engine._GeoIndex(data)

    def test_bare_resolves_to_city_not_region(self):
        result = engine._resolve_bare("washington", self.index)
        self.assertEqual(result["city"], "Washington")
        self.assertEqual(result["region"], "DC")

    def test_comma_qualified_still_resolves_region_via_tail_context(self):
        result = engine._resolve_segments(["seattle", "washington"], self.index)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Seattle")
        self.assertEqual(result["region"], "WA")


class SameTypeCityTiebreakTests(SimpleTestCase):
    """Covers AE2 -- exercised against a fixture _GeoIndex, not v1/v2.yaml,
    since v1.yaml has no city-name collisions to test the tiebreak against."""

    def setUp(self):
        self.index = engine._GeoIndex(_TIEBREAK_DATA)

    def test_bare_same_type_collision_resolves_to_highest_population(self):
        # Massachusetts (155929 pop, PPL tier 9) beats Illinois (114394 pop,
        # PPLA tier 1) -- population leads the tiebreak (see engine.py's
        # _best_city_candidate docstring for the real-data evidence behind
        # this priority: feature-code-first picked a small foreign admin
        # seat over a much larger, more likely-intended city for a real
        # "San Francisco" lookup during implementation spot-checks).
        result = engine._resolve_bare("springfield", self.index)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "MA")

    def test_same_population_falls_through_to_feature_code_tier(self):
        data = {
            **_TIEBREAK_DATA,
            "cities": [
                {**c, "population": 100000} for c in _TIEBREAK_DATA["cities"]
            ],
        }
        index = engine._GeoIndex(data)
        result = engine._resolve_bare("springfield", index)
        # IL is PPLA (tier 1), the highest tier among the three once
        # population is tied.
        self.assertEqual(result["region"], "IL")

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

    def test_cross_country_abbrev_collision_resolves_via_country_population(self):
        # Real GeoNames collision: "CA" is both California's postal code and
        # Luxembourg's Capellen district code. region_any_by_alias is
        # list-valued specifically so this doesn't silently drop the alias
        # (which would break the very common "City, ST" pattern) or
        # last-write-wins on whichever was loaded second -- California's
        # ~327M-person country beats Luxembourg's ~600K decisively.
        result = engine._resolve_segments(["milpitas", "ca"], self.index)
        self.assertEqual(result["region"], "CA")
        self.assertEqual(result["country"], "US")

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


class BareAliasNoRegressionTests(SimpleTestCase):
    """Covers U4's pre-cutover regression check: a sample of v1.yaml's bare,
    uniquely-resolved city aliases resolve under v2 to the *same* city --
    not merely "still resolved." v1 never had a same-type collision to
    exercise the tiebreak against, so this is the concrete proof the
    tiebreak doesn't silently pick a different candidate for input that
    previously had no ambiguity to resolve at all."""

    def test_v1_bare_resolved_cities_match_v2(self):
        v1_index = engine._load_index("v1")
        for alias in ("london", "toronto", "chicago", "munich", "bangalore"):
            with self.subTest(alias=alias):
                v1_result = engine._resolve_bare(alias, v1_index)
                v2_result = normalize_location(alias)
                self.assertTrue(v1_result["resolved"])
                self.assertTrue(v2_result["resolved"])
                self.assertEqual(v1_result["country"], v2_result["country"])


class FeatureCodeTierTests(SimpleTestCase):
    def test_capital_beats_admin_seat(self):
        self.assertLess(engine.feature_code_tier("PPLC"), engine.feature_code_tier("PPLA"))

    def test_missing_code_sorts_last(self):
        self.assertGreater(engine.feature_code_tier(None), engine.feature_code_tier("PPLA5"))


class StringFormatFixesTests(SimpleTestCase):
    """Covers AE3, AE4 -- exercised against the real, currently-loaded
    dataset (v2.yaml as of U4's version bump)."""

    def test_area_suffix_stripped(self):
        # GeoNames' canonical name for this city is "Bengaluru" (its actual
        # official name since 2014); "bangalore" is still a recognized
        # alias, which is what lets the input string match at all.
        result = normalize_location("Bangalore Area")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Bengaluru")

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
