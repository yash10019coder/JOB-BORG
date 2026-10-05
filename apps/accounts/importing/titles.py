"""A small, versioned list of job-title words (rules EX7, FN3, TL).

Used to tell a *role* ("Senior Backend Engineer") from an *employer* ("Acme
Corporation") when a resume puts them on neighbouring lines in either order, and
to stop a headline such as "Software Engineer" being taken for a person's name.

Authored content: reviewed in the pull request that changes it, and
``TITLE_LEXICON_VERSION`` is bumped on every change.
"""
import re

TITLE_LEXICON_VERSION = "2026-10-1"

TITLE_WORDS = (
    "engineer", "engineering", "developer", "intern", "internship", "analyst", "manager", "lead",
    "architect", "consultant", "designer", "scientist", "associate", "director", "head",
    "specialist", "administrator", "researcher", "contributor", "founder", "co-founder", "cofounder",
    "trainee", "officer", "executive", "programmer", "tester", "coordinator", "assistant", "fellow",
    "mentor", "freelancer", "freelance", "volunteer", "member", "representative", "sde", "swe",
    "sre", "devops", "qa", "vp", "cto", "ceo", "cfo", "coo", "president", "principal", "staff",
    "technician", "strategist", "editor", "writer", "advisor", "adviser", "owner", "supervisor",
    "apprentice", "instructor", "teacher", "professor", "lecturer", "recruiter", "accountant",
    "auditor", "operator", "scrum master", "product owner", "evangelist", "ambassador",
)

_TITLE_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(word) for word in sorted(TITLE_WORDS, key=len, reverse=True)) + r")s?(?![A-Za-z])",
    re.IGNORECASE,
)


def has_title_word(text):
    """True if the text contains a job-title word (``Backend Engineer``)."""
    return bool(_TITLE_RE.search(text or ""))
