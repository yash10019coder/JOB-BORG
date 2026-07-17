"""Declarative rule-type evaluators for the classification engine.

Each evaluator takes ``(rule, job)`` and returns a bool. ``job`` is a plain
snapshot dict (title, description, location, is_remote, salary_min, salary_max).
Rules are data — no per-job LLM calls, no I/O.
"""
import re


class RuleConfigError(ValueError):
    """A ruleset contains a malformed or unknown rule."""


def _search_text(job):
    return f"{job.get('title', '')}\n{job.get('description', '')}".lower()


def keyword_any(rule, job):
    """Match if any keyword appears as a (case-insensitive) substring."""
    text = _search_text(job)
    return any(kw.lower() in text for kw in rule["keywords"])


def keyword_all(rule, job):
    """Match only if every keyword appears."""
    text = _search_text(job)
    return all(kw.lower() in text for kw in rule["keywords"])


def regex(rule, job):
    """Match a regex against the search text (case-insensitive, word-aware)."""
    return re.search(rule["pattern"], _search_text(job), re.IGNORECASE) is not None


def field_equals(rule, job):
    """Match if a named job field equals the rule's value."""
    return job.get(rule["field"]) == rule["value"]


def salary_threshold(rule, job):
    """Match if a numeric salary field is present and >= the rule's minimum."""
    value = job.get(rule["field"])
    if value is None:
        return False
    return value >= rule["min"]


RULE_EVALUATORS = {
    "keyword_any": keyword_any,
    "keyword_all": keyword_all,
    "regex": regex,
    "field_equals": field_equals,
    "salary_threshold": salary_threshold,
}


def evaluate(rule, job):
    try:
        evaluator = RULE_EVALUATORS[rule["type"]]
    except KeyError as exc:
        raise RuleConfigError(f"Unknown rule type: {rule.get('type')!r}") from exc
    return evaluator(rule, job)
