"""The classification engine: run a declarative ruleset over a job snapshot.

Deterministic and side-effect-free. A ruleset is versioned so the periodic
re-sampling pass (an offline human activity that derives new rules) can swap in
a new version without code changes.
"""
from functools import lru_cache
from pathlib import Path

import yaml

from .rule_types import RuleConfigError, evaluate

RULESETS_DIR = Path(__file__).resolve().parent / "rulesets"

# Ruleset version applied by default (bump when a new ruleset is promoted).
CURRENT_RULESET_VERSION = "v1"


@lru_cache(maxsize=None)
def load_ruleset(version=CURRENT_RULESET_VERSION):
    """Load and cache a ruleset by version. Raises RuleConfigError if missing."""
    path = RULESETS_DIR / f"{version}.yaml"
    if not path.exists():
        raise RuleConfigError(f"No ruleset file for version {version!r}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "rules" not in data:
        raise RuleConfigError(f"Ruleset {version!r} is malformed (no 'rules' key)")
    return data


def classify(job, ruleset=None):
    """Return the sorted, deduplicated list of tags for ``job``.

    ``job`` is a snapshot dict. ``ruleset`` may be a loaded ruleset dict; when
    omitted the current version is used. Output is canonical (sorted) so
    identical input always yields identical output regardless of rule order.
    """
    if ruleset is None:
        ruleset = load_ruleset()
    tags = set()
    for rule in ruleset["rules"]:
        if evaluate(rule, job):
            tags.add(rule["tag"])
    return sorted(tags)
