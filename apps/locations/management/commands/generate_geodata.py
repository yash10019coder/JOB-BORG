"""Regenerate apps/locations/geodata/v2.yaml from GeoNames' raw exports.

Offline, one-shot: fetches (or reads locally-provided copies of)
cities15000.txt, admin1CodesASCII.txt, and countryInfo.txt, transforms them
via apps.locations.geodata_generation, and writes the checked-in YAML file.
Not invoked at runtime -- normalize_location() only ever reads the
already-generated, already-committed file.
"""
import datetime

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.locations.geodata_generation import (
    build_geodata,
    parse_admin1_file,
    parse_cities_file,
    parse_countries_file,
    render_yaml,
)

CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
COUNTRIES_URL = "https://download.geonames.org/export/dump/countryInfo.txt"


class Command(BaseCommand):
    help = "Regenerate apps/locations/geodata/v2.yaml from GeoNames' exports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cities-file", help="Local path to a cities15000.txt (skip download)."
        )
        parser.add_argument(
            "--admin1-file", help="Local path to admin1CodesASCII.txt (skip download)."
        )
        parser.add_argument(
            "--countries-file", help="Local path to countryInfo.txt (skip download)."
        )
        parser.add_argument(
            "--output", help="Output path (defaults to apps/locations/geodata/v2.yaml)."
        )

    def handle(self, *args, **options):
        cities_text = self._read_or_fetch(options["cities_file"], CITIES_URL, zipped=True)
        admin1_text = self._read_or_fetch(options["admin1_file"], ADMIN1_URL)
        countries_text = self._read_or_fetch(options["countries_file"], COUNTRIES_URL)

        city_rows = parse_cities_file(cities_text)
        admin1_map = parse_admin1_file(admin1_text)
        country_rows = parse_countries_file(countries_text)

        data = build_geodata(city_rows, admin1_map, country_rows)
        yaml_text = render_yaml(data, download_date=datetime.date.today().isoformat())

        output_path = options["output"] or (
            settings.BASE_DIR / "apps" / "locations" / "geodata" / "v2.yaml"
        )
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {output_path}: {len(data['countries'])} countries, "
                f"{len(data['regions'])} regions, {len(data['cities'])} cities, "
                f"{len(data['ambiguous_bare_tokens'])} ambiguous tokens"
            )
        )

    def _read_or_fetch(self, local_path, url, *, zipped=False):
        if local_path:
            with open(local_path, "r", encoding="utf-8") as f:
                return f.read()

        response = requests.get(url, timeout=60)
        response.raise_for_status()
        if not zipped:
            return response.text

        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            inner_name = next(n for n in zf.namelist() if n.endswith(".txt"))
            return zf.read(inner_name).decode("utf-8")
