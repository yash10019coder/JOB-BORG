"""Compatibility re-export: the implementation moved to ``apps.accounts.regions``
(the resolver in ``apps.accounts`` needs it and must not import ``apps.web``)."""
from apps.accounts.regions import *  # noqa: F401,F403
from apps.accounts.regions import (  # noqa: F401
    COUNTRY_ALPHA3_TO_REGION,
    COUNTRY_NAME_TO_REGION,
    REGION_KEYS,
    REGION_LABELS,
    alpha3_country_map,
    country_choices,
    label_for_country,
    region_for_country,
)
