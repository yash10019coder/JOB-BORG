"""Compatibility re-export: the implementation moved to ``apps.accounts.salary_bands``."""
from apps.accounts.salary_bands import *  # noqa: F401,F403
from apps.accounts.salary_bands import (  # noqa: F401
    DEFAULT_SALARY_BANDS,
    SALARY_BANDS_BY_REGION,
    get_all_band_keys,
    get_salary_band_label,
    is_valid_band_key,
    validate_salary_by_region,
)
