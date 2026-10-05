"""Proposals and the grounding check (rules G1-G3, FN, FL, FK).

Every proposed value carries the spans of the (normalized) document text it came
from. ``is_grounded`` recomputes the value from those spans with the *same*
deriver the extractor used; if the result differs, or a span is out of range, the
proposal is dropped. So a value that is not literally supported by the text can
never reach the review page, whichever code produced it (rules or, later, an LLM).
"""
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from apps.accounts.importing import skills_lexicon
from apps.accounts.importing.titles import has_title_word
from apps.accounts.importing.validators import (
    ImportValueError,
    clean_phone,
    clean_text,
    clean_titles,
    clean_url,
)

SNIPPET_CHARS = 80

# Role/level tags are never imported from a skills list (FK2).
ROLE_TAGS = frozenset(
    {"backend", "frontend", "data", "design", "devops", "senior", "junior", "staff",
     "remote", "high_comp"}
)

_NAME_TOKEN = re.compile(r"^[^\W\d_](?:[^\W\d_]|['’.-])*$")
_CONTACT_LABELS = frozenset(
    {"email", "e-mail", "mail", "phone", "mobile", "contact", "tel", "ph", "linkedin", "github"}
)
# "Jane Doe Resume" / "Jane Doe CV" page headers: the trailing label is dropped too.
_TRAILING_LABELS = _CONTACT_LABELS | {"resume", "cv"}
_NAME_STOP = frozenset(
    {"resume", "curriculum", "vitae", "cv", "profile", "summary", "contact", "page", "address",
     "objective", "experience", "education", "skills"}
)


@dataclass(frozen=True)
class Proposal:
    field: str
    value: object
    spans: tuple  # ((start, end), ...) into the normalized text
    snippet: str
    extractor: str = "rule"
    provisional: bool = False  # layout not yet verified on a real sample


def make_snippet(text, start, end, width=SNIPPET_CHARS):
    """≤ 80 characters of the document around a span, for the review page (G3)."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    line = text[line_start:line_end]
    if len(line) <= width:
        return line.strip()
    centre = (start - line_start + end - line_start) // 2
    left = max(0, min(centre - width // 2, len(line) - width))
    return line[left:left + width].strip()


# --------------------------------------------------------------------------
# Derivers: raw span text -> the value (shared by extraction and grounding)
# --------------------------------------------------------------------------
def derive_name(raw):
    """FN2-FN4: 2-4 letter tokens, no contact label, not a job title, not a
    heading word; ALL CAPS becomes Title Case; all-lowercase is rejected."""
    # Symbols and icons (phone/envelope glyphs, emoji) become spaces; punctuation
    # such as a comma stays, so "New Delhi, India" can never pass as a name.
    cleaned = "".join(" " if unicodedata.category(ch)[0] in "SC" else ch for ch in raw)
    tokens = cleaned.split()
    while tokens and tokens[-1].casefold().rstrip(":.") in _TRAILING_LABELS:
        tokens.pop()
    if not 2 <= len(tokens) <= 4 or not all(_NAME_TOKEN.match(t) for t in tokens):
        raise ImportValueError("bad_name")
    if sum(len(re.sub(r"\W", "", t)) >= 2 for t in tokens) < 2:
        raise ImportValueError("bad_name")
    if {t.casefold().strip(".") for t in tokens} & _NAME_STOP or has_title_word(" ".join(tokens)):
        raise ImportValueError("bad_name")
    name = " ".join(tokens)
    if name.islower():
        raise ImportValueError("bad_name")
    if name.isupper():
        name = " ".join(t.title() for t in tokens)
    return clean_text(name, max_len=255, min_len=3)


def derive_location(raw):
    """FL1-FL4: ``(city, alpha3-or-None)`` for a ``City, Region[, Country]`` token
    that the location engine resolves to a city. Skill and job-title words never
    count as a place (``Python, Java`` must not resolve to Java, Indonesia)."""
    from apps.locations.engine import alpha3_for_country, normalize_location

    raw = raw.strip()
    parts = [p.strip() for p in raw.split(",")]
    if not 2 <= len(parts) <= 3 or re.search(r"[\d@]", raw):
        raise ImportValueError("bad_location")
    for part in parts:
        if skills_lexicon.canonical_skill(part) or has_title_word(part):
            raise ImportValueError("bad_location")
    result = normalize_location(raw)
    if not result or not result.get("resolved") or not result.get("city"):
        raise ImportValueError("bad_location")
    country = result.get("country")
    return result["city"], (alpha3_for_country(country) if country else None)


@lru_cache(maxsize=1)
def live_tags():
    """Tags the classification ruleset can actually produce (read at runtime)."""
    from apps.classification.engine import load_ruleset

    return frozenset(rule["tag"] for rule in load_ruleset()["rules"])


def tag_for_skill(raw):
    """FK1-FK2: the job-matching tag for a skill spelling, or ``None``. Only tags
    that exist in the live ruleset and are not role/level tags are returned."""
    key = re.sub(r"\s+", " ", (raw or "").strip()).casefold()
    tag = skills_lexicon.TAG_ALIASES.get(key)
    return tag if tag in live_tags() and tag not in ROLE_TAGS else None


def _derive_tags(raws):
    out = []
    for raw in raws:
        tag = tag_for_skill(raw)
        if tag is None:
            raise ImportValueError("not_a_tag")
        if tag not in out:
            out.append(tag)
    return out


def _derive_portfolio(raw):
    value = clean_url(raw)
    host = value.split("/")[2]
    if host in ("linkedin.com", "www.linkedin.com", "github.com", "www.github.com"):
        raise ImportValueError("own_profile_link")
    return value


DERIVERS = {
    "full_name": lambda raws: derive_name(raws[0]),
    "phone": lambda raws: clean_phone(raws[0].strip()),
    "linkedin_url": lambda raws: clean_url(raws[0], kind="linkedin"),
    "github_url": lambda raws: clean_url(raws[0], kind="github"),
    "portfolio_url": lambda raws: _derive_portfolio(raws[0]),
    "location_city": lambda raws: derive_location(raws[0])[0],
    "location_country": lambda raws: derive_location(raws[0])[1],
    "target_tags": _derive_tags,
    "headline": lambda raws: clean_text(raws[0], max_len=255, min_len=3),
    "current_employer": lambda raws: clean_text(raws[0], max_len=255, min_len=2),
    "target_titles": lambda raws: clean_titles(raws),
}


def is_grounded(text, proposal):
    """G1: every span lies inside the text and re-deriving the value from the
    spans gives exactly the proposed value."""
    deriver = DERIVERS.get(proposal.field)
    if deriver is None or not proposal.spans:
        return False
    raws = []
    for start, end in proposal.spans:
        if not (0 <= start < end <= len(text)):
            return False
        raws.append(text[start:end])
    try:
        return deriver(raws) == proposal.value
    except (ImportValueError, ValueError):
        return False
