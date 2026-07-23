"""Tests for the GeoNames -> v2.yaml transformation logic -- pure, no DB."""
from django.test import SimpleTestCase

from apps.locations.engine import feature_code_tier
from apps.locations.geodata_generation import (
    build_geodata,
    parse_admin1_file,
    parse_cities_file,
    parse_countries_file,
    render_yaml,
)

# Real-shaped sample rows (tab-separated), trimmed to the columns tests need
# but keeping every column position so the parser's indexing is exercised
# honestly. Columns: geonameid, name, asciiname, alternatenames, lat, lon,
# feature class, feature code, country code, cc2, admin1, admin2, admin3,
# admin4, population, elevation, dem, timezone, mod date.
LONDON_ROW = (
    "2643743\tLondon\tLondon\tLondres,LON,London\t51.50853\t-0.12574\tP\tPPLC\t"
    "GB\t\tENG\tGLA\t\t\t8961989\t\t25\tEurope/London\t2023-08-04"
)
SPRINGFIELD_IL_ROW = (
    "4250542\tSpringfield\tSpringfield\tSpringfield\t39.80172\t-89.64371\tP\tPPLA\t"
    "US\t\tIL\t167\t\t\t114230\t\t180\tAmerica/Chicago\t2019-09-16"
)
SPRINGFIELD_MA_ROW = (
    "4951788\tSpringfield\tSpringfield\tSpringfield\t42.10148\t-72.58981\tP\tPPL\t"
    "US\t\tMA\t013\t\t\t155929\t\t21\tAmerica/New_York\t2019-09-16"
)
SMALL_TOWN_ROW = (
    "1\tTinyville\tTinyville\t\t0\t0\tP\tPPL\tUS\t\tNY\t\t\t\t500\t\t0\tAmerica/New_York\t2020-01-01"
)

ADMIN1_TEXT = "US.IL\tIllinois\tIllinois\t4896861\nUS.MA\tMassachusetts\tMassachusetts\t6254926\nGB.ENG\tEngland\tEngland\t6269131\n"

COUNTRIES_TEXT = (
    "# comment line, skipped\n"
    "US\tUSA\t840\tUS\tUnited States\tWashington\t9629091\t327167434\tNA\t.us\tUSD\tDollar\t1\t#####-####\t^\\d{5}(-\\d{4})?$\ten-US\t6252001\tCA,MX\t\n"
    "GB\tGBR\t826\tUK\tUnited Kingdom\tLondon\t244820\t66488991\tEU\t.uk\tGBP\tPound\t44\t\t\ten-GB\t2635167\tIE\t\n"
)


class ParseCitiesFileTests(SimpleTestCase):
    def test_parses_real_shaped_row(self):
        rows = parse_cities_file(LONDON_ROW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "London")
        self.assertEqual(row["country_code"], "GB")
        self.assertEqual(row["admin1_code"], "ENG")
        self.assertEqual(row["population"], 8961989)
        self.assertEqual(row["feature_code"], "PPLC")
        self.assertIn("Londres", row["alternatenames"])

    def test_below_min_population_excluded(self):
        rows = parse_cities_file(SMALL_TOWN_ROW, min_population=15000)
        self.assertEqual(rows, [])

    def test_min_population_threshold_is_inclusive_boundary_respected(self):
        rows = parse_cities_file(SMALL_TOWN_ROW, min_population=500)
        self.assertEqual(len(rows), 1)


class ParseAdmin1FileTests(SimpleTestCase):
    def test_parses_code_to_name_mapping(self):
        result = parse_admin1_file(ADMIN1_TEXT)
        self.assertEqual(result["US.IL"], "Illinois")
        self.assertEqual(result["GB.ENG"], "England")


class ParseCountriesFileTests(SimpleTestCase):
    def test_parses_iso_and_name_skipping_comments(self):
        rows = parse_countries_file(COUNTRIES_TEXT)
        self.assertEqual(len(rows), 2)
        us = next(r for r in rows if r["iso"] == "US")
        self.assertEqual(us["iso3"], "USA")
        self.assertEqual(us["name"], "United States")


class FeatureCodeTierTests(SimpleTestCase):
    def test_capital_ranks_above_admin_seat(self):
        self.assertLess(feature_code_tier("PPLC"), feature_code_tier("PPLA"))

    def test_unknown_code_ranks_last(self):
        self.assertGreater(feature_code_tier("PPL"), feature_code_tier("PPLA5"))


class BuildGeodataTests(SimpleTestCase):
    def _build(self, city_text):
        city_rows = parse_cities_file(city_text, min_population=1)
        admin1_map = parse_admin1_file(ADMIN1_TEXT)
        country_rows = parse_countries_file(COUNTRIES_TEXT)
        return build_geodata(city_rows, admin1_map, country_rows, min_population=1)

    def test_country_display_names_use_v1_overrides(self):
        data = self._build(LONDON_ROW)
        names = {c["name"] for c in data["countries"]}
        self.assertIn("US", names)
        self.assertIn("UK", names)
        self.assertNotIn("GB", names)

    def test_uk_alias_present_on_gb_country_for_r8(self):
        data = self._build(LONDON_ROW)
        uk = next(c for c in data["countries"] if c["name"] == "UK")
        self.assertIn("uk", uk["aliases"])

    def test_city_gets_population_and_feature_code(self):
        data = self._build(LONDON_ROW)
        london = next(c for c in data["cities"] if c["name"] == "London")
        self.assertEqual(london["population"], 8961989)
        self.assertEqual(london["feature_code"], "PPLC")

    def test_city_region_joined_via_admin1_map(self):
        data = self._build(LONDON_ROW)
        london = next(c for c in data["cities"] if c["name"] == "London")
        self.assertEqual(london["region"], "ENG")

    def test_missing_admin1_code_produces_none_region_not_a_crash(self):
        no_admin1_row = LONDON_ROW.replace("\tENG\t", "\t\t")
        data = self._build(no_admin1_row)
        london = next(c for c in data["cities"] if c["name"] == "London")
        self.assertIsNone(london["region"])

    def test_alternatename_colliding_with_a_country_alias_is_filtered(self):
        # Real GeoNames data quality issue: the Serbian town "Inđija" lists
        # "India" among its alternatenames, which would otherwise mark the
        # colliding country alias cross-type ambiguous and break resolution
        # for the whole country. A city's PRIMARY name/asciiname is never
        # filtered this way -- only alternatenames-sourced aliases are held
        # to the stricter bar (see geodata_generation._build_cities
        # docstring). Uses "usa" (already in the fixture's COUNTRIES_TEXT)
        # as the colliding alias, standing in for the real "india" case.
        parts = LONDON_ROW.split("\t")
        parts[1] = "Indjija"
        parts[2] = "Indjija"
        parts[3] = "usa,Indjija"
        fake_city_row = "\t".join(parts)
        data = self._build(fake_city_row)
        self.assertNotIn("usa", data["ambiguous_bare_tokens"])
        indjija = next(c for c in data["cities"] if c["name"] == "Indjija")
        self.assertNotIn("usa", indjija["aliases"])
        self.assertIn("indjija", indjija["aliases"])

    def test_airport_code_looking_alternatename_is_filtered(self):
        data = self._build(LONDON_ROW)
        london = next(c for c in data["cities"] if c["name"] == "London")
        self.assertNotIn("lon", london["aliases"])
        self.assertIn("londres", london["aliases"])

    def test_same_type_city_collision_kept_resolvable_not_ambiguous(self):
        combined = LONDON_ROW + "\n" + SPRINGFIELD_IL_ROW + "\n" + SPRINGFIELD_MA_ROW
        data = self._build(combined)
        self.assertNotIn("springfield", data["ambiguous_bare_tokens"])
        springfields = [c for c in data["cities"] if c["name"] == "Springfield"]
        self.assertEqual(len(springfields), 2)

    def test_cross_type_country_vs_region_collision_marked_ambiguous(self):
        # Country vs. region (no city involved) has no "which one is
        # overwhelmingly more common" precedent the way country-vs-city or
        # region-vs-city do -- and the origin brainstorm's success criteria
        # requires this exact homograph to stay unresolved -- so it's the
        # one case that still fails closed.
        admin1_text = ADMIN1_TEXT + "US.UK\tUK\tUK\t9999999\n"
        city_rows = parse_cities_file(LONDON_ROW, min_population=1)
        admin1_map = parse_admin1_file(admin1_text)
        country_rows = parse_countries_file(COUNTRIES_TEXT)
        data = build_geodata(city_rows, admin1_map, country_rows, min_population=1)
        self.assertIn("uk", data["ambiguous_bare_tokens"])
        country_aliases = next(c for c in data["countries"] if c["name"] == "UK")["aliases"]
        self.assertNotIn("uk", country_aliases)

    def test_same_type_region_collision_across_countries_marked_ambiguous(self):
        # Two different (country, region) pairs both named "England"-alike
        # via a contrived admin1 map collision.
        city_rows = parse_cities_file(LONDON_ROW, min_population=1)
        admin1_map = {"GB.ENG": "Central", "US.IL": "Central"}
        country_rows = parse_countries_file(COUNTRIES_TEXT)
        data = build_geodata(city_rows, admin1_map, country_rows, min_population=1)
        self.assertIn("central", data["ambiguous_bare_tokens"])
        regions_named_central = [r for r in data["regions"] if r["name"] == "Central"]
        self.assertTrue(regions_named_central)
        for region in regions_named_central:
            self.assertNotIn("central", region["full_aliases"])

    def test_unique_region_full_alias_not_marked_ambiguous(self):
        data = self._build(LONDON_ROW)
        self.assertNotIn("england", data["ambiguous_bare_tokens"])
        england = next(r for r in data["regions"] if r["name"] == "England")
        self.assertIn("england", england["full_aliases"])

    def test_region_vs_city_same_name_no_country_involved_prefers_city(self):
        # Real, high-impact GeoNames pattern: "New York" the state and New
        # York City share a bare name, as do "Washington" the state and
        # Washington, D.C. v1.yaml's own curation deliberately dropped the
        # region's claim so the city (the overwhelmingly common real-world
        # meaning) wins, rather than failing the token closed entirely.
        admin1_text = ADMIN1_TEXT + "US.NY\tNew York\tNew York\t5128638\n"
        city_rows = parse_cities_file(LONDON_ROW, min_population=1)
        admin1_map = parse_admin1_file(admin1_text)
        country_rows = parse_countries_file(COUNTRIES_TEXT)
        # Contrive a city literally named "New York" to collide with the
        # "New York" region full_alias.
        fake_city_rows = [{**city_rows[0], "name": "New York", "asciiname": "New York"}]
        data = build_geodata(fake_city_rows, admin1_map, country_rows, min_population=1)

        self.assertNotIn("new york", data["ambiguous_bare_tokens"])
        ny_region = next(r for r in data["regions"] if r["name"] == "New York")
        self.assertNotIn("new york", ny_region["full_aliases"])
        ny_city = next(c for c in data["cities"] if c["name"] == "New York")
        self.assertIn("new york", ny_city["aliases"])
        # Comma-context ("Some City, New York") has no collision at all --
        # demoting the bare claim must not also break this extremely common
        # pattern (confirmed as a real regression on production data during
        # implementation: dropping the alias outright broke 250+ real
        # "City, Washington" rows).
        self.assertIn("new york", ny_region["comma_context_full_aliases"])

    def test_country_vs_city_same_name_no_region_involved_prefers_country(self):
        # Real GeoNames pattern: city-states like Singapore are both a
        # country and their own city entry. _resolve_bare already checks
        # country before city, so this case needs no exclusion at all --
        # only the region-vs-city case (above) needs active intervention.
        countries_text = COUNTRIES_TEXT + "SG\tSGP\t702\tSG\tSingapore\tSingapore\t710\t5638676\tAS\t.sg\tSGD\tDollar\t65\t\t\ten-SG,ms-SG\t1880251\t\t\n"
        parts = LONDON_ROW.split("\t")
        parts[1] = "Singapore"
        parts[2] = "Singapore"
        parts[8] = "SG"
        parts[10] = ""
        fake_city_row = "\t".join(parts)
        city_rows = parse_cities_file(fake_city_row, min_population=1)
        admin1_map = parse_admin1_file(ADMIN1_TEXT)
        country_rows = parse_countries_file(countries_text)
        data = build_geodata(city_rows, admin1_map, country_rows, min_population=1)

        self.assertNotIn("singapore", data["ambiguous_bare_tokens"])
        singapore = next(c for c in data["countries"] if c["name"] == "SG")
        self.assertIn("singapore", singapore["aliases"])

    def test_country_iso_code_colliding_with_region_abbrev_dropped_from_country(self):
        # Real GeoNames collision: "GA" is both Gabon's ISO alpha-2 code and
        # a US state's abbreviation. Without this exclusion,
        # _resolve_segments' tail lookup ("Atlanta, GA") would confidently
        # resolve country=Gabon instead of country=US/region=GA, since
        # country_by_alias is checked before region_any_by_alias.
        countries_text = COUNTRIES_TEXT + "GA\tGAB\t266\tGB\tGabon\tLibreville\t267668\t2119275\tAF\t.ga\tXAF\tFranc\t241\t\t\tfr-GA,fang,myene\t2400001\tCG,CM,GQ\t\n"
        admin1_text = ADMIN1_TEXT + "US.GA\tGeorgia\tGeorgia\t4197000\n"
        city_rows = parse_cities_file(LONDON_ROW, min_population=1)
        admin1_map = parse_admin1_file(admin1_text)
        country_rows = parse_countries_file(countries_text)
        data = build_geodata(city_rows, admin1_map, country_rows, min_population=1)

        gabon = next(c for c in data["countries"] if c["name"] == "GA")
        self.assertNotIn("ga", gabon["aliases"])

        georgia = next(r for r in data["regions"] if r["name"] == "Georgia")
        self.assertIn("ga", georgia["abbrev_aliases"])


class RenderYamlTests(SimpleTestCase):
    def test_output_is_loadable_yaml_with_attribution_header(self):
        import yaml

        city_rows = parse_cities_file(LONDON_ROW, min_population=1)
        admin1_map = parse_admin1_file(ADMIN1_TEXT)
        country_rows = parse_countries_file(COUNTRIES_TEXT)
        data = build_geodata(city_rows, admin1_map, country_rows, min_population=1)
        text = render_yaml(data, download_date="2026-07-23")

        self.assertIn("GeoNames", text)
        self.assertIn("CC-BY 4.0", text)
        self.assertIn("2026-07-23", text)

        loaded = yaml.safe_load(text)
        self.assertEqual(loaded["version"], "v2")
        self.assertTrue(loaded["cities"])
