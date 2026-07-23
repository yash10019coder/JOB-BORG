# Credits

## Location data

`apps/locations/geodata/v2.yaml` is derived from [GeoNames](https://www.geonames.org/)
data (`cities15000.txt`, `admin1CodesASCII.txt`, `countryInfo.txt`), licensed under
[Creative Commons Attribution 4.0 International (CC-BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

The GeoNames export data has been transformed (filtered, re-shaped, and merged with
GeoNames' own feature-code and population fields) into a versioned lookup dataset via
`manage.py generate_geodata`. See `apps/locations/geodata/v2.yaml`'s header comment for
the download date and source files used.
