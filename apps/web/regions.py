"""Region membership and country<->region mapping for salary bands.

The salary bands in :mod:`apps.web.salary_bands` are keyed by region, but a
job only tells us a *country*. This module is the single place that decides
which region a country belongs to, so :mod:`apps.web.salary_bands` and
``drafting.py`` never each keep their own copy of that decision (the two
disagreeing copies are what let this silently stop working).

**Countries are identified by ISO 3166-1 alpha-3.** That is deliberate, and it
is the fix for a bug that made the feature effectively dead:

* ``apps.locations``'s canonical country strings are a *mix* -- bare ISO-2
  for some countries ("US", "SG", "Australia" -> "AU") and full names for
  others ("UK", "Germany", "India", "Canada"). So alpha-2 is not a usable key
  for a single mapping table.
* ``engine.country_by_prefix_code`` deliberately omits alpha-2 codes that
  collide with a US state postal abbreviation ("CA", "IN", "DE" --
  California, Indiana, Delaware), so alpha-2 can't bridge those either.
* Alpha-3 is present and collision-free for 248 of the 252 countries in the
  checked-in dataset; the remaining 4 come from
  ``engine.country_alpha3_supplement``. See ``apps.locations.engine`` for how
  ``country_records()`` assembles all 252.

A previous hand-written mapping keyed on alpha-2 resolved only **16 of 252**
countries -- and specifically *not* India, the UK, Canada, Germany or the
Netherlands, i.e. most of the regions a user picks from, so no salary band was
proposed for them at all. Keying on alpha-3, with coverage asserted against
the dataset in ``tests/test_regions.py``, is what makes this trustworthy.

**Countries with no region are left unmapped, deliberately.** Membership is
restricted to countries whose own currency the region's bands are quoted in.
A job in a country with no band data (Japan, Brazil, Mexico, ...) resolves to
``None`` and no band is proposed, rather than one quoted in the wrong
currency -- the same "a confidently wrong resolution is worse than an
unresolved one" invariant ``apps.locations.engine`` holds to. Extend
``_REGION_MEMBERSHIP`` to add coverage.
"""

from __future__ import annotations

from apps.locations.engine import alpha3_for_country, country_records

# Region key -> (display label, ISO 3166-1 alpha-3 members).
#
# The band *currency* for each region lives in
# ``salary_bands.SALARY_BANDS_BY_REGION`` (its labels carry the symbol); it
# is not repeated here. Membership is the currency boundary described in the
# module docstring, with one documented exception: the "Europe" tab the user
# asked for is deliberately a single tab, so non-euro EEA/EFTA members (PL,
# SE, DK, CZ, HU, RO, BG, ...) are included even though they quote in their own
# currency -- one Europe market beats seven near-identical tabs.
_REGION_MEMBERSHIP: dict[str, tuple[str, tuple[str, ...]]] = {
    "US": ("United States", ("USA",)),
    "CA": ("Canada", ("CAN",)),
    # Crown dependencies quote in GBP.
    "UK": ("United Kingdom", ("GBR", "JEY", "GGY", "IMN")),
    # NZD bands track AUD closely enough to share one market tab.
    "AU": ("Australia", ("AUS", "NZL")),
    "IN": ("India", ("IND",)),
    "SG": ("Singapore", ("SGP",)),
    "EU": (
        "Europe",
        (
            "AUT", "BEL", "BGR", "CHE", "CYP", "CZE", "DEU", "DNK", "ESP",
            "EST", "FIN", "FRA", "GRC", "HRV", "HUN", "IRL", "ISL", "ITA",
            "LIE", "LTU", "LUX", "LVA", "MCO", "MLT", "NLD", "NOR", "POL",
            "PRT", "ROU", "SVK", "SVN", "SWE",
        ),
    ),
}

# ISO alpha-3 -> region key.
COUNTRY_ALPHA3_TO_REGION: dict[str, str] = {
    alpha3: region_key
    for region_key, (_label, members) in _REGION_MEMBERSHIP.items()
    for alpha3 in members
}

#: Region key -> human label, for rendering the region tabs/headers.
REGION_LABELS: dict[str, str] = {key: label for key, (label, _) in _REGION_MEMBERSHIP.items()}

#: Region keys in display order.
REGION_KEYS: tuple[str, ...] = tuple(REGION_LABELS)

_ALPHA3_TO_NAME: dict[str, str] = {}
_NAME_TO_ALPHA3: dict[str, str] = {}
for _record in country_records():
    _ALPHA3_TO_NAME[_record["alpha3"]] = _record["name"]
    _NAME_TO_ALPHA3[_record["name"].lower()] = _record["alpha3"]

#: The engine's canonical country string -- what ``normalize_location()`` puts
#: in a ``Job.target_locations_normalized`` entry's ``"country"`` -- to region.
COUNTRY_NAME_TO_REGION: dict[str, str] = {
    name: COUNTRY_ALPHA3_TO_REGION[alpha3]
    for alpha3, name in _ALPHA3_TO_NAME.items()
    if alpha3 in COUNTRY_ALPHA3_TO_REGION
}

# alpha-2 -> alpha-3 is deliberately NOT built here. The locations dataset
# omits every alpha-2 colliding with a US state postal abbreviation (CA, DE,
# IN, ...), so a table that appeared to cover them would have to be a second,
# hand-maintained country list -- exactly what let the previous mapping
# silently resolve only 16 of 252 countries. Use ``engine.alpha3_for_country``
# instead, and store alpha-3.

_LABEL_BY_NAME: dict[str, str] = {
    record["name"]: record["label"] for record in country_records()
}


def country_choices() -> list[tuple[str, str]]:
    """``(alpha3, human label)`` pairs for every country, sorted by label.

    The value is the ISO alpha-3 code because that is what
    :data:`COUNTRY_ALPHA3_TO_REGION` is keyed on and what
    ``Profile.visa_status_by_country`` / ``Profile.citizenship_countries``
    store. Storing the code rather than the display string keeps those rows
    valid across a dataset rename.
    """
    return [(record["alpha3"], record["label"]) for record in country_records()]


def alpha3_country_map() -> dict[str, str]:
    """``{alpha3: engine canonical country name}`` for every country."""
    return dict(_ALPHA3_TO_NAME)


def label_for_country(value: str) -> str:
    """Human label for a country given in any form
    ``engine.alpha3_for_country`` accepts (alpha-3, the engine's canonical
    name, a full name). Falls back to ``value`` unchanged when unrecognized,
    so this degrades to something readable rather than blank."""
    alpha3 = alpha3_for_country(value)
    if alpha3 and alpha3 in _ALPHA3_TO_NAME:
        return _LABEL_BY_NAME[_ALPHA3_TO_NAME[alpha3]]
    return value


def region_for_country(value: str) -> str | None:
    """Region key for a country given in any form
    ``engine.alpha3_for_country`` accepts -- the engine's canonical country
    string ("UK", "Germany", "US"), an alpha-3 code ("GBR", "DEU"), or a full
    name ("Singapore") -- or ``None``.

    Accepting all of those is what makes callers agree regardless of which
    form they hold; notably a job whose country normalized to "Germany"
    resolves to "EU" exactly as one held as "DEU" would.

    ``None`` means *no salary band is available for this country* and callers
    must not fall back to another region's bands. Note that ISO alpha-2 codes
    colliding with a US state postal abbreviation ("CA", "DE", "IN") resolve to
    ``None`` by design -- see :func:`apps.locations.engine.alpha3_for_country`.
    """
    alpha3 = alpha3_for_country(value)
    if alpha3:
        return COUNTRY_ALPHA3_TO_REGION.get(alpha3)
    return COUNTRY_NAME_TO_REGION.get((value or "").strip())
