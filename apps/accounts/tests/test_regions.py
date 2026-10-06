from django.test import SimpleTestCase

from apps.accounts import regions, salary_bands
from apps.web import regions as web_regions
from apps.web import salary_bands as web_salary_bands


class ShimTests(SimpleTestCase):
    """The old `apps.web` import paths keep working after the move."""

    def test_old_paths_re_export_the_same_objects(self):
        self.assertIs(web_regions.region_for_country, regions.region_for_country)
        self.assertIs(web_regions.country_choices, regions.country_choices)
        self.assertIs(web_regions.REGION_KEYS, regions.REGION_KEYS)
        self.assertIs(web_salary_bands.SALARY_BANDS_BY_REGION, salary_bands.SALARY_BANDS_BY_REGION)
        self.assertIs(
            web_salary_bands.validate_salary_by_region, salary_bands.validate_salary_by_region
        )

    def test_dead_alpha2_mapping_is_gone(self):
        self.assertFalse(hasattr(salary_bands, "_map_country_to_region"))
        self.assertFalse(hasattr(salary_bands, "_get_salary_band_label"))


class RegionForCountryTests(SimpleTestCase):
    def test_known_countries_in_any_accepted_form(self):
        self.assertEqual(regions.region_for_country("IND"), "IN")
        self.assertEqual(regions.region_for_country("India"), "IN")
        self.assertEqual(regions.region_for_country("UK"), "UK")
        self.assertEqual(regions.region_for_country("Germany"), "EU")
        self.assertEqual(regions.region_for_country("US"), "US")

    def test_countries_without_band_data_have_no_region(self):
        self.assertIsNone(regions.region_for_country("Japan"))
        self.assertIsNone(regions.region_for_country(""))

    def test_state_colliding_alpha2_codes_do_not_resolve(self):
        for code in ("CA", "DE", "IN"):
            self.assertIsNone(regions.region_for_country(code), code)

    def test_every_region_has_bands_and_a_label(self):
        for key in regions.REGION_KEYS:
            self.assertIn(key, salary_bands.SALARY_BANDS_BY_REGION)
            self.assertTrue(regions.REGION_LABELS[key])
