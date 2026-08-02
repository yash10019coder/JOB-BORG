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

    def test_structurally_malformed_dataset_raises_location_data_error_not_keyerror(self):
        # Regression: _try_load_index only caught LocationDataError, so a
        # structurally-malformed-but-dict-shaped dataset (e.g. a country
        # entry missing its required "name" key) used to raise an uncaught
        # KeyError, violating normalize_location's documented never-raise
        # contract instead of surfacing as the defined "unresolved" fallback.
        engine._load_index.cache_clear()
        bad_data = {"countries": [{"aliases": ["us"], "population": 1}]}
        try:
            with mock.patch("yaml.safe_load", return_value=bad_data):
                with mock.patch("pathlib.Path.exists", return_value=True):
                    with mock.patch("pathlib.Path.read_text", return_value=""):
                        with self.assertRaises(LocationDataError):
                            engine._load_index("bogus-version-for-malformed-test")
        finally:
            engine._load_index.cache_clear()

    def test_normalize_location_stays_unresolved_not_raising_on_malformed_dataset(self):
        engine._load_index.cache_clear()
        bad_data = {"countries": [{"aliases": ["us"], "population": 1}]}
        try:
            with mock.patch("yaml.safe_load", return_value=bad_data):
                with mock.patch("pathlib.Path.exists", return_value=True):
                    with mock.patch("pathlib.Path.read_text", return_value=""):
                        result = normalize_location("New York, NY, US")
        finally:
            engine._load_index.cache_clear()
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": False},
        )


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
    previously had no ambiguity to resolve at all.

    v1.yaml never populated `region` outside the US, and its display names
    predate GeoNames canonicalization (e.g. "Bangalore" vs v2's official
    "Bengaluru") -- so v1's own output isn't a reliable ground truth for
    city/region equality across versions. Country equality against v1 is
    still a meaningful cross-version check; city/region are instead
    asserted directly against v2's known-correct values, which is the
    actual proof the tiebreak didn't silently pick a different candidate.
    """

    EXPECTED_V2 = {
        "london": {"city": "London", "region": "ENG", "country": "UK"},
        "toronto": {"city": "Toronto", "region": "ON", "country": "Canada"},
        "chicago": {"city": "Chicago", "region": "IL", "country": "US"},
        "munich": {"city": "Munich", "region": "02", "country": "Germany"},
        "bangalore": {"city": "Bengaluru", "region": "19", "country": "India"},
    }

    def test_v1_bare_resolved_cities_match_v2(self):
        v1_index = engine._load_index("v1")
        for alias, expected in self.EXPECTED_V2.items():
            with self.subTest(alias=alias):
                v1_result = engine._resolve_bare(alias, v1_index)
                v2_result = normalize_location(alias)
                self.assertTrue(v1_result["resolved"])
                self.assertTrue(v2_result["resolved"])
                self.assertEqual(v1_result["country"], v2_result["country"])
                self.assertEqual(v2_result["city"], expected["city"])
                self.assertEqual(v2_result["region"], expected["region"])
                self.assertEqual(v2_result["country"], expected["country"])


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

    def test_hybrid_alone_is_no_place_info_not_unresolved(self):
        # "hybrid" is deliberately NOT in REMOTE_MARKERS (a hybrid job is
        # not remote, and REMOTE_MARKERS also drives is_remote elsewhere) --
        # but it IS one of _LOCATION_NOISE_WORDS_RE's words (see
        # LocationNoiseWordStrippingTests), which exists specifically so a
        # bare work-arrangement word with no place info reduces to
        # _NO_PLACE_INFO rather than being flagged as a coverage gap.
        result = normalize_location("Hybrid")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": True},
        )

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

    def test_prefix_country_scopes_the_remainder_instead_of_being_discarded(self):
        # Regression for a real bug: the prefix's identified country used to
        # be discarded once it confirmed the remainder had *some* match
        # anywhere in the world, letting a same-type population tiebreak
        # pick a city in a completely different country. "NZ - Cambridge"
        # must resolve to Cambridge, New Zealand, not Cambridge, England
        # (which would win a global population tiebreak).
        result = normalize_location("NZ - Cambridge")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "NZ")

    def test_prefix_country_with_no_matching_remainder_stays_unresolved(self):
        # "Berlin" resolves globally (to Germany), but Mexico has no city
        # by that name in the dataset -- silently falling back to the
        # global tiebreak would discard the explicit "MX" hint and
        # confidently resolve to the wrong country. Must stay unresolved.
        result = normalize_location("MX - Berlin")
        self.assertFalse(result["resolved"])


class BreadcrumbHierarchyDelimiterTests(SimpleTestCase):
    """Regression for a real gap: some ATSes emit a "Country > [Region >]
    City" breadcrumb -- biggest-to-smallest, the opposite order of this
    module's comma convention (city, ..., country) -- which _split_segments
    used to treat as one unsplittable bare token (no comma present), so it
    never matched anything and stayed unresolved. Confirmed against real
    production data: 45 distinct unresolved strings, all this exact shape,
    exercised here against the real, currently-loaded dataset."""

    def test_two_level_breadcrumb_resolves_country_and_city(self):
        result = normalize_location("China > Shanghai")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Shanghai")
        self.assertEqual(result["country"], "CN")

    def test_three_level_breadcrumb_resolves_city_region_and_country(self):
        result = normalize_location("US > Arizona > Phoenix")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Phoenix")
        self.assertEqual(result["region"], "AZ")
        self.assertEqual(result["country"], "US")

    def test_breadcrumb_country_alias_added_by_the_taiwan_fix_also_works(self):
        result = normalize_location("Taiwan > Tainan")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "Taiwan")


class PrefixCodeLookupTests(SimpleTestCase):
    """R8's dedicated country_by_prefix_code lookup, exercised against a
    fixture index rather than the real dataset -- reproduces the confirmed
    "SG - Singapore" bug (SG absent from country_by_alias due to an
    incidental region-abbrev collision, but present in the dedicated prefix
    lookup) and the "GA - Atlanta" US-state-collision exclusion, independent
    of whether the checked-in dataset has been regenerated yet."""

    def setUp(self):
        self.data = {
            "countries": [
                # "sg" and "singapore" deliberately absent from aliases --
                # simulates the real collision-drop this dedicated lookup
                # exists to fix. Resolution can only succeed by (a) using
                # country_iso2_prefixes to recognize "sg" as a prefix at
                # all, which strips it so the remainder becomes bare
                # "singapore", and (b) scoping the bare city match to SG.
                {"name": "SG", "aliases": ["sgp"], "population": 5638676},
                # "ga" IS present in country_by_alias here (unlike the real
                # dataset, where it's dropped by the US-state-collision
                # exclusion) specifically so the next test can prove R8
                # doesn't fall back to country_by_alias for a code the
                # dedicated prefix lookup excludes -- without this entry,
                # the test would pass identically whether or not such a
                # fallback existed.
                {"name": "Gabon", "aliases": ["ga", "gab"], "population": 2119275},
            ],
            "regions": [],
            "cities": [
                {
                    "name": "Singapore",
                    "region": None,
                    "country": "SG",
                    "population": 5638676,
                    "feature_code": "PPLC",
                    "aliases": ["singapore"],
                }
            ],
            "ambiguous_bare_tokens": [],
            "country_iso2_prefixes": {"sg": "SG"},
        }
        self.patcher = mock.patch.object(
            engine, "_load_index", return_value=engine._GeoIndex(self.data)
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_prefix_resolves_via_dedicated_lookup_despite_alias_collision(self):
        result = normalize_location("SG - Singapore")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "SG")

    def test_code_absent_from_prefix_lookup_stays_unresolved(self):
        # "ga" is deliberately absent from country_iso2_prefixes (simulating
        # the US-state exclusion) but IS present in country_by_alias (see
        # setUp) -- if R8 fell back to country_by_alias for an excluded
        # code, this would resolve to Gabon. It must not.
        result = normalize_location("GA - Atlanta")
        self.assertFalse(result["resolved"])

    def test_country_first_dash_scan_also_defers_two_letter_code_to_r8(self):
        # Regression caught by the test suite: _reorder_country_first_dash
        # (the "<Country full name> - City" scan) must never match a
        # 2-letter dash segment via its own country_by_alias lookup, even
        # though "ga" resolves to Gabon there in this fixture -- that
        # lookup has no US-state-collision exclusion, so without a length
        # guard it silently resolved "GA - Atlanta" to Gabon instead of
        # correctly deferring to R8, which already ran and correctly left
        # it unresolved.
        result = normalize_location("GA - Atlanta")
        self.assertFalse(result["resolved"])


class AreaSuffixSameTypeCollisionTests(SimpleTestCase):
    """Regression for a real bug: R7's "<X> Area" suffix strip fed its
    result directly into the unconstrained same-type bare-city tiebreak,
    letting a generic word ("Bay", "Metro", "Delta", "North") that happens
    to coincidentally match an obscure real place resolve confidently to
    the wrong city. "Bay Area" is an especially common real-world string
    for SF Bay Area job postings."""

    def test_generic_area_descriptor_stays_unresolved(self):
        for raw in ("Bay Area", "Metro Area", "Delta Area", "North Area", "Greater Boston Area"):
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertFalse(result["resolved"])

    def test_unambiguous_suffix_stripped_city_still_resolves(self):
        # The fix must not regress the case this suffix-stripping exists
        # for: a real, unambiguous city name with "Area" appended.
        result = normalize_location("Bangalore Area")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Bengaluru")

    def test_bare_generic_descriptor_without_area_suffix_stays_unresolved(self):
        # Regression: _GENERIC_AREA_DESCRIPTORS was only consulted when
        # `strict` was set, which only happened when R7's "Area" suffix was
        # actually stripped -- a bare generic word with no suffix skipped
        # the check entirely and confidently resolved to an obscure
        # namesake place (e.g. "Metro" -> Metro, Indonesia; "Delta" ->
        # Delta, Canada; "Downtown" -> Śródmieście, Poland).
        for raw in ("Metro", "Delta", "Bay", "Downtown", "North", "Uptown"):
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertFalse(result["resolved"])

    def test_extended_generic_area_descriptors_stay_unresolved(self):
        # Adversarially-discovered generic descriptors added to the
        # blocklist: "Shore Area", "North Shore Area", "Piedmont Area",
        # "Gold Coast Area", "Highlands Area" all previously resolved
        # confidently to an obscure namesake place.
        for raw in (
            "Shore Area",
            "North Shore Area",
            "Piedmont Area",
            "Gold Coast Area",
            "Highlands Area",
        ):
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertFalse(result["resolved"])

    def test_disambiguable_same_type_collision_still_resolves_via_population(self):
        # These are real, non-generic city names that happen to share a
        # name with a much smaller namesake elsewhere -- not the "generic
        # word coincidentally matches an obscure place" failure mode this
        # class guards against. The population tiebreak (same one used for
        # the non-strict same-type-collision path) should still apply.
        cases = {
            "Paris Area": ("Paris", "FR"),
            "Madrid Area": ("Madrid", "ES"),
            "Getafe Area": ("Getafe", "ES"),
        }
        for raw, (city, country) in cases.items():
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertTrue(result["resolved"])
                self.assertEqual(result["city"], city)
                self.assertEqual(result["country"], country)

    def test_metro_qualifier_before_area_is_stripped(self):
        # Real production pattern: 141 unresolved rows used the French
        # spelling "Metropolitain" (Rome/Berlin/London, 47 each) directly
        # before "Area" -- R7 only stripped "Area" itself, leaving a
        # two-word bare token ("Rome Metropolitain") that never matches
        # any single-token city alias.
        cases = {
            "Rome Metropolitain Area": ("Rome", "IT"),
            "Berlin Metropolitain Area": ("Berlin", "Germany"),
            "London Metropolitain Area": ("London", "UK"),
            "Berlin Metropolitan Area": ("Berlin", "Germany"),
        }
        for raw, (city, country) in cases.items():
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertTrue(result["resolved"])
                self.assertEqual(result["city"], city)
                self.assertEqual(result["country"], country)

    def test_bare_metro_area_with_no_city_qualifier_still_unresolved(self):
        # Must not regress to "no place info" (resolved=True, all fields
        # None) -- "Metro Area" alone still has no place info a curator
        # could add, so it stays the existing unresolved-generic-descriptor
        # outcome, not a silent status flip.
        result = normalize_location("Metro Area")
        self.assertFalse(result["resolved"])


class PrefixCountryPreservedThroughRemoteStrippingTests(SimpleTestCase):
    """Regression: "<CC> - <remote marker>" used to discard an already-
    identified prefix_country once the remainder stripped to empty,
    regressing e.g. "US - Remote" to NO_PLACE_INFO with country=None even
    though "Remote - US" (differently ordered) correctly resolved country."""

    def test_country_prefix_before_remote_marker_preserves_country(self):
        result = normalize_location("US - Remote")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "US")
        self.assertIsNone(result["city"])

    def test_uk_country_prefix_before_remote_marker_preserves_country(self):
        result = normalize_location("UK - Remote")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "UK")
        self.assertIsNone(result["city"])


class ScopeCountryEnforcedForCountryAndRegionBranchesTests(SimpleTestCase):
    """Regression: R8's `scope_country` hint was only enforced in
    _resolve_bare's city-matching branch -- the country and region branches
    returned immediately without consulting it, letting a mismatched prefix
    discard the poster's own hint (e.g. "NZ - Germany" resolving to Germany,
    "NZ - Texas" resolving to Texas, US)."""

    def test_prefix_country_mismatched_with_resolved_country_stays_unresolved(self):
        result = normalize_location("NZ - Germany")
        self.assertFalse(result["resolved"])

    def test_prefix_country_mismatched_with_resolved_region_stays_unresolved(self):
        result = normalize_location("NZ - Texas")
        self.assertFalse(result["resolved"])


class PipeMultiLocationDelimiterTests(SimpleTestCase):
    """Real production gap: 581 unresolved rows used "|" to list multiple
    candidate locations (e.g. "San Jose | San Francisco"), a delimiter
    normalize_location never recognized -- the whole string was tried as
    one bare token and failed. The fallback tries the whole string first
    (see normalize_location's docstring-adjacent comment) and only then
    tries each "|"-segment in turn, so it can only rescue an
    otherwise-unresolved row, never override a working resolution --
    confirmed necessary by a real regression where an earlier version that
    unconditionally split on "|" and took only the first segment discarded
    a later segment that carried the only real place info in the string."""

    def test_first_of_pipe_delimited_list_resolves(self):
        result = normalize_location("San Jose | San Francisco")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "San Jose")

    def test_pipe_with_surrounding_noise_still_resolves_first_entry(self):
        result = normalize_location("London | New York")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "London")

    def test_noise_leading_segment_falls_through_to_later_resolvable_one(self):
        # Real production string. The whole-string attempt already resolves
        # this via the comma-tail machinery reading straight through
        # "Remote-Friendly (Travel Required)" to "CA" -- the "|" fallback
        # must never run here, and if it did (e.g. by only trying the first
        # segment), it would discard "San Francisco, CA" entirely and lose
        # the row instead of merely giving a coarser (city=None) result.
        result = normalize_location("Remote-Friendly (Travel Required) | San Francisco, CA")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "CA")
        self.assertEqual(result["country"], "US")

    def test_noise_only_segment_does_not_win_over_a_later_real_place(self):
        # P0 regression: a leading "|"-segment that resolves via
        # _NO_PLACE_INFO (pure noise, e.g. "Remote") has resolved=True the
        # same as a genuine place match -- the fallback loop must not let
        # it win the race and silently discard a later segment that
        # resolves to a real city. Confirmed as a real bug:
        # normalize_location("Remote | San Francisco HQ") used to return
        # {city: None, region: None, country: None, resolved: True}
        # instead of San Francisco.
        cases = [
            "Remote | San Francisco HQ",
            "San Francisco HQ | Remote",
            "Hybrid | Austin",
            "Distributed | Chicago",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertTrue(result["resolved"])
                self.assertIsNotNone(result["city"])

    def test_all_noise_segments_still_resolve_to_no_place_info(self):
        # When every "|"-segment is pure noise, the string must still
        # resolve as no-place-info (matching a single bare "Remote"'s
        # behavior), not fall through to unresolved.
        result = normalize_location("Remote | Hybrid")
        self.assertTrue(result["resolved"])
        self.assertIsNone(result["city"])
        self.assertIsNone(result["region"])
        self.assertIsNone(result["country"])

    def test_literal_pipe_surviving_into_comma_tail_still_resolves_city(self):
        # Regression: when the tail segment alone already resolves (e.g.
        # "ga" -> Georgia, US), the whole string is resolved=True before
        # the "|" fallback above ever gets a chance to run -- so a literal
        # "|" left inside the *head* segment ("ga | atlanta") must not
        # prevent the city half from resolving. Confirmed as a real bug:
        # normalize_location("GA | Atlanta, GA") used to return
        # {city: None, region: "GA", country: "US", resolved: True},
        # silently losing "Atlanta".
        result = normalize_location("GA | Atlanta, GA")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Atlanta")
        self.assertEqual(result["region"], "GA")
        self.assertEqual(result["country"], "US")


class CanadaAustraliaRegionCodeParityTests(SimpleTestCase):
    """Regression: a resolved Canadian/Australian city's "region" field
    used to carry the raw GeoNames numeric admin1 code ("08", "02") instead
    of the postal abbreviation the same job posting would have typed and
    the abbrev-alias lookup already matched against ("ON", "NSW") -- unlike
    a US city, whose region code already IS its postal abbreviation.
    Confirmed on the real generated v3 dataset before this fix."""

    def test_canadian_city_region_is_postal_abbreviation(self):
        result = normalize_location("Toronto, ON")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "ON")

    def test_australian_city_region_is_postal_abbreviation(self):
        result = normalize_location("Sydney, NSW")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "NSW")


class TrailingBareCodeFallbackTests(SimpleTestCase):
    """Real production gap: 671 unresolved rows used a bare "<City> <ST>"
    shape with no comma or dash at all (e.g. "Atlanta GA", "Tempe AZ") --
    _resolve_bare tried the whole string as one token and failed."""

    def test_city_with_trailing_state_code_resolves(self):
        cases = {
            "Atlanta GA": ("Atlanta", "GA", "US"),
            "Tempe AZ": ("Tempe", "AZ", "US"),
        }
        for raw, (city, region, country) in cases.items():
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertTrue(result["resolved"])
                self.assertEqual(result["city"], city)
                self.assertEqual(result["region"], region)
                self.assertEqual(result["country"], country)

    def test_trailing_non_code_token_stays_unresolved(self):
        # "hq" isn't a real region/country alias -- the fallback must fail
        # harmlessly rather than inventing a match.
        result = normalize_location("Emeryville HQ")
        self.assertFalse(result["resolved"])

    def test_already_resolvable_bare_city_unaffected(self):
        # The fallback only runs after the plain bare-token lookup already
        # failed -- a normal single-word city must resolve exactly as
        # before, not get re-parsed as (word, trailing-2-letters).
        result = normalize_location("Berlin")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Berlin")


class CountryFirstDashOrderTests(SimpleTestCase):
    """Real production gap: hundreds of unresolved rows used a "<Country
    full name> - <City>" order (e.g. "Brazil - Rio de Janeiro", "India -
    Bengaluru") -- the opposite of this module's comma convention (city,
    ..., country). Distinct from R8, which only recognizes a 2-letter ISO
    code prefix, not a full country name."""

    def test_country_first_dash_resolves_city_and_country(self):
        cases = {
            "Brazil - Rio de Janeiro": ("Rio de Janeiro", "Brazil"),
            "India - Bengaluru": ("Bengaluru", "India"),
            "Canada - Toronto": ("Toronto", "Canada"),
        }
        for raw, (city, country) in cases.items():
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                self.assertTrue(result["resolved"])
                self.assertEqual(result["city"], city)
                self.assertEqual(result["country"], country)

    def test_leading_noise_segment_before_country_is_skipped(self):
        # "APAC" isn't a country -- the scan must skip past it to find
        # "Australia" rather than giving up at the first segment.
        result = normalize_location("APAC - Australia - Sydney")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Sydney")

    def test_non_country_leading_segment_stays_on_normal_pipeline(self):
        # Neither "Retail" nor "New York" is a country alias -- must not
        # misfire and must fall through to the ordinary (unresolved)
        # outcome rather than raising or guessing.
        result = normalize_location("Retail - New York")
        self.assertFalse(result["resolved"])

    def test_iso_prefix_still_takes_precedence(self):
        # R8's 2-letter-code prefix path runs first and consumes the
        # dash -- must not regress once the new full-name check is added.
        result = normalize_location("US - Chicago")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Chicago")
        self.assertEqual(result["country"], "US")


class TrailingParentheticalFallbackTests(SimpleTestCase):
    """Real production gap: 2000+ unresolved rows had a trailing
    parenthetical -- sometimes pure noise ("Brasov (30 Hermann)"), so it
    must be tried as a fallback (only after the full string already
    failed), never as the first attempt -- a parenthetical can also carry
    a genuine disambiguating hint that must not be discarded when the
    unparenthesized string would already resolve."""

    def test_noise_parenthetical_stripped_on_fallback(self):
        result = normalize_location("Bangkok (Central World Office)")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Bangkok")

    def test_comma_qualified_location_with_trailing_noise_paren(self):
        result = normalize_location("Toronto, ON (Canada)")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Toronto")

    def test_already_resolved_string_not_retried_without_parenthetical(self):
        # If the full string (parenthetical included) already resolves,
        # the fallback must never run -- confirmed by asserting the
        # parenthetical's own content wouldn't have produced this result
        # (Germany is redundant with Munich's own population-tiebreak
        # resolution here, not the source of it).
        result = normalize_location("Munich (Germany)")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Munich")
        self.assertEqual(result["country"], "Germany")


class AbbreviationPeriodStrippingTests(SimpleTestCase):
    """Real production gap: "Washington, D.C." (57 rows) stayed unresolved
    while "Washington, DC" already worked -- GeoNames-derived region
    aliases are period-free ("dc"), so period-preserving input silently
    never matched its own dataset."""

    def test_dc_with_periods_resolves(self):
        result = normalize_location("Washington, D.C.")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Washington")
        self.assertEqual(result["region"], "DC")

    def test_iso_prefix_with_periods_still_recognized(self):
        # Periods must be stripped before R8's prefix regex runs, not
        # after, or "U.S. - Chicago" would never match the 2-letter anchor.
        result = normalize_location("U.S. - Chicago")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["city"], "Chicago")
        self.assertEqual(result["country"], "US")


class LocationNoiseWordStrippingTests(SimpleTestCase):
    """Real production gap, two-sided: (1) office/company noise words
    stripped from an otherwise-real place unlock resolution ("New York
    Office" -> New York, 82 rows), and (2) a string that's pure noise with
    no place info at all ("Hybrid", "Location TBD", "2 Locations") should
    land in the existing _NO_PLACE_INFO state, not be flagged as an
    unresolved coverage gap when there was never a place to find. Kept
    deliberately separate from REMOTE_MARKERS (used by
    apps/jobs/ingestion/normalizers.py's is_remote derivation) -- "hybrid",
    "office", "hq" must never be treated as implying is_remote=True."""

    def test_office_suffix_unlocks_real_city(self):
        cases = {
            "New York Office": "New York City",
            "Paris Offices": "Paris",
            "SF Office": "San Francisco",
            "Emeryville HQ": None,  # below the dataset's population cutoff
        }
        for raw, expected_city in cases.items():
            with self.subTest(raw=raw):
                result = normalize_location(raw)
                if expected_city is None:
                    self.assertFalse(result["resolved"])
                else:
                    self.assertTrue(result["resolved"])
                    self.assertEqual(result["city"], expected_city)

    def test_pure_noise_strings_are_no_place_info_not_unresolved(self):
        no_place_info = {"city": None, "region": None, "country": None, "resolved": True}
        for raw in (
            "Hybrid",
            "Location TBD",
            "2 Locations",
            "Distributed",
            "In-Office",
            "On-Site",
            "Global",
            "Nationwide",
            "Multiple Cities",
            "Multiple Locations",
            "Any",
            "Any CEO Office",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_location(raw), no_place_info)

    def test_remotely_based_does_not_falsely_resolve_to_libya(self):
        # Regression: REMOTE_MARKERS' substring-replace strips "remote" out
        # of "remotely", leaving the fragment "ly" -- which is Libya's ISO
        # alpha-2 code. Stripping "based" as a noise word too would leave
        # "ly" as the sole remaining bare token, confidently (and wrongly)
        # resolving to Libya. Must stay unresolved instead.
        result = normalize_location("Remotely based")
        self.assertFalse(result["resolved"])
        self.assertIsNone(result["country"])

    def test_bare_region_code_left_after_noise_word_strip_resolves(self):
        # Regression: stripping "Hybrid" from "Hybrid - MI" leaves the bare
        # abbrev "MI" with no head/city at all, which _resolve_bare
        # previously never checked (only full region names, e.g.
        # "michigan", not abbrev codes) -- this used to work only via the
        # trailing-bare-code fallback's two-token (head, tail) shape, which
        # noise-word stripping broke by consuming the "head".
        result = normalize_location("Hybrid - MI")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "MI")
        self.assertEqual(result["country"], "US")

    def test_bare_abbrev_fallback_does_not_reopen_the_gabon_georgia_case(self):
        # The bare-abbrev-region fallback above must be gated on a noise
        # word actually having been stripped -- a token the user typed
        # directly, bare, with nothing discarded, must NOT gain this new
        # capability, or "GA" alone would resolve to Georgia, US despite
        # also being Gabon's ISO code (see NormalizeLocationTests.
        # test_bare_abbreviation_alone_unresolved and the "GA - Atlanta"
        # precedent this mirrors).
        result = normalize_location("GA")
        self.assertFalse(result["resolved"])


class TrailingZipCodeTests(SimpleTestCase):
    """Real production gap: full-address-shaped location strings (e.g.
    "510 East 62nd Street, New York, NY 10065", 47 rows) never resolved --
    the comma-tail segment "ny 10065" matched neither a country nor a
    region alias verbatim, discarding a perfectly good regional signal."""

    def test_us_zip_stripped_from_tail_segment(self):
        result = normalize_location("510 East 62nd Street, New York, NY 10065")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "NY")
        self.assertEqual(result["country"], "US")

    def test_zip_plus_four_also_stripped(self):
        result = normalize_location("New York, NY 10065-1234")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["region"], "NY")

    def test_tail_that_is_only_a_zip_code_not_collapsed_to_empty(self):
        # Guards the "leaves a non-empty remainder" check -- a tail that's
        # nothing but digits must not silently vanish and fall through to
        # some unrelated resolution.
        result = normalize_location("Somewhere, 10065")
        self.assertFalse(result["resolved"])


class RegionalBusinessTermNoiseWordTests(SimpleTestCase):
    """Real production gap: "Home based - Worldwide"/"-EMEA"/"-Americas"
    (93+83+30 rows), bare "North America" (79 rows), "Europe" (34 rows),
    and "Latin America" (17 rows) are business-region descriptors, never
    real place info -- same _NO_PLACE_INFO rationale as the other noise
    words in LocationNoiseWordStrippingTests."""

    def test_business_region_terms_are_no_place_info(self):
        no_place_info = {"city": None, "region": None, "country": None, "resolved": True}
        for raw in (
            "Home based - Worldwide",
            "Home based - EMEA",
            "Home Based - Americas",
            "North America",
            "Europe",
            "Latin America",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_location(raw), no_place_info)

    def test_remoto_recognized_as_a_remote_marker(self):
        # Spanish/Portuguese for "remote" -- genuinely implies remote work
        # (unlike "hybrid"/"office"), so unlike those it belongs in
        # REMOTE_MARKERS itself, not the separate noise-word list.
        result = normalize_location("Remoto")
        self.assertEqual(
            result,
            {"city": None, "region": None, "country": None, "resolved": True},
        )

    def test_brasil_synonym_resolves(self):
        result = normalize_location("Brasil")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["country"], "Brazil")
