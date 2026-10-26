"""Rule-based question-category classifier (compatibility re-export).

The implementation moved to ``apps.accounts.tiering`` -- a dependency-free
leaf shared with the answer resolver, which must not import ``apps.auto_apply``.
"""
from apps.accounts.tiering import (  # noqa: F401
    HARD_EXCLUDED_CATEGORIES,
    QuestionCategory,
    classify,
)
