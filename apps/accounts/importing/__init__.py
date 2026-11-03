"""Profile import (Phase 4): safe document reading, rule-based extraction and
grounding, kept as pure, dependency-free helpers.

Rules live in ``docs/plans/2026-10-05-002-profile-import-rules.md``; each module
cites the rule IDs it implements. ``apps.accounts`` code must not import
``apps.auto_apply`` or ``apps.web``.
"""
