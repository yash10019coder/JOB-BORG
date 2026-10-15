"""Is one application question a plausible *candidate* for another?

A pure, deterministic leaf (like ``apps.accounts.tiering`` and
``apps.accounts.question_semantics``): regexes and set arithmetic over two
strings, no models, no network, no ``apps.auto_apply`` / ``apps.web`` imports.
The retrieval service (``apps.accounts.services.question_candidates``) uses it
to decide which of a user's earlier answered questions to show next to a
reworded one.

A match here is only ever a *suggestion* shown to the reviewer. It never keys,
fills or confirms an answer. The guiding rule of this effort still holds --
**a confidently wrong candidate is worse than none** -- so everything here
fails closed: a guard vetoes a pair before any score is looked at, and for the
legal and commercial tiers (and for anything the classifier does not
recognise, which it files as legal) two questions match only when their
meaningful words are identical or differ only by wording we have listed as
interchangeable for that topic.

Rules (cited by tests; see docs/plans/2026-10-06-002-question-matching-rules.md):

* QM1  Comparison text: lowercase, decoration removed, bounded length.
* QM2  Trigram similarity is Jaccard over padded character trigrams.
* QM3  ``typed_owned``: a question the typed layer owns never gets a candidate.
* QM4  ``polarity``: a differing set of negation words vetoes the pair.
* QM5  ``auth_kind``: authorization vs sponsorship (or any ambiguous
       authorization wording) vetoes the pair.
* QM6  ``countries``: differing named countries or country abbreviations veto.
* QM7  ``tier``: differing risk tiers veto, unless the two share an intent, in
       which case the riskier tier governs (the classifier knows fewer wordings
       than the intent table does).
* QM8  ``numbers``: differing digit strings veto ("at least 18" / "at least 21").
* QM8b ``named_terms``: a capitalised term in one question that appears nowhere
       in the other (Python / Java, Canadian / British) vetoes the pair, so one
       swapped name never merges two questions.
* QM8c ``intent``: two different recognised intents veto.
* QM9  ``strict_tier``: for T0/T1 (and unknown) pairs, equal meaningful words,
       or the same intent with only that intent's interchangeable wording
       differing; anything else is no match.
* QM10 Intents: an ordered table of topics; compound text has no intent.
* QM11 Outside the strict tiers a pair matches on trigram score at or above the
       threshold, or on equal intent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from apps.accounts import question_semantics as qs
from apps.accounts.tiering import Tier, classify_tier, higher_tier

# QM1: questions are short; anything longer is cut before scoring so a hostile
# or pathological text cannot cost real CPU.
MAX_COMPARE_CHARS = 300

# QM11: tuned on the labelled set (see ``question_match_eval``).
DEFAULT_MIN_SCORE = 0.5

_STRICT_TIERS = frozenset({Tier.T0_LEGAL, Tier.T1_COMMERCIAL})

# --------------------------------------------------------------------------
# QM1: comparison text
# --------------------------------------------------------------------------

_DECORATION = re.compile(r"\((?:optional|required)\)")
_CONTRACTION = re.compile(r"n['’]t\b")
_NON_WORD = re.compile(r"[^\w]+")


def comparable(text) -> str:
    """Lowercased, decoration-free text used only for comparison."""
    text = str(text or "")[: MAX_COMPARE_CHARS * 2].lower()
    text = _DECORATION.sub(" ", text)
    text = _CONTRACTION.sub(" not", text)
    text = _NON_WORD.sub(" ", text).strip()
    text = re.sub(r"^please\s+", "", text)
    return text[:MAX_COMPARE_CHARS]


# Function words that carry no topic. "us" is deliberately NOT here: it is also
# the country.
_STOP = frozenset(
    "a an the of to in on at for with by or and is are was were be been am do does did "
    "you your yours i me my we our have has had this that these those it its as if "
    "how what which when where who whom whose why please also would will can could should "
    "may might shall".split()
)

# QM4
_NEGATIONS = frozenset({"not", "no", "never", "without", "none", "cannot", "nor", "neither"})


def _words(text: str) -> list[str]:
    return text.split()


def _content(words) -> frozenset[str]:
    return frozenset(w for w in words if w not in _STOP and w not in _NEGATIONS)


def _numbers(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+", text))


# --------------------------------------------------------------------------
# QM2: trigram similarity
# --------------------------------------------------------------------------


def trigrams(text: str) -> frozenset[str]:
    """Padded character trigrams of every word (the ``pg_trgm`` convention)."""
    grams = set()
    for word in _words(text):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of two comparison texts' trigram sets, 0..1."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------
# Authorization vs sponsorship, for pairing only
# --------------------------------------------------------------------------

_IMMIGRATION = re.compile(r"\bimmigration\b", re.IGNORECASE)


def _auth_kind(raw: str):
    """``question_semantics.authorization_kind`` with the bare word "immigration"
    removed. That word makes a question "ambiguous" for the *typed fact* layer
    (an immigration *status* needs a person), but "Do you require immigration
    sponsorship?" is still plainly the sponsorship question, and pairing it
    with the other sponsorship wordings is exactly what this module is for.
    H-1B, work permits and citizenship stay ambiguous."""
    return qs.authorization_kind(_IMMIGRATION.sub(" ", raw))


# --------------------------------------------------------------------------
# QM10: intents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Intent:
    name: str
    pattern: re.Pattern
    # Words that may differ between two questions of this intent without
    # changing what is asked (QM9).
    allowed: frozenset[str]


# Wording that never changes what is asked, whatever the topic: the verb that
# introduces the question and the word for the job being applied to.
_COMMON_ALLOWED = frozenset(
    "indicate select provide state enter choose tell specify role position job "
    "opportunity company".split()
)


def _intent(name, pattern, allowed):
    return _Intent(name, re.compile(pattern, re.IGNORECASE), _COMMON_ALLOWED | frozenset(allowed.split()))


_INTENTS = (
    _intent(
        "notice_period",
        r"\bnotice period\b|\bhow much notice\b|\bperiod of notice\b|\bnotice\b.{0,30}\b(?:give|serve|required|need)",
        "notice period current employer give need required much long serve currently employment contract weeks days months",
    ),
    _intent(
        "start_date",
        r"\bstart date\b|\bwhen (?:can|could|are) you (?:start|begin|join)\b|"
        r"\bavailab(?:le|ility) to (?:start|join)\b|\bearliest\b.{0,30}\bstart",
        "start date earliest available availability join begin possible preferred expected anticipated",
    ),
    _intent(
        "relocation",
        r"\brelocat",
        "relocate relocating relocation willing open able comfortable move assistance",
    ),
    _intent(
        "referral_source",
        r"\bhow did you (?:hear|find|learn)\b|\bwhere did you (?:hear|find|learn)\b|\bhear(?:d)? about\b",
        "hear heard find found learn learned about us role position job opportunity company "
        "where first out source",
    ),
    _intent(
        "gender",
        r"\bgender\b|\bsex\b",
        "gender identity identify describe sex self identification indicate select",
    ),
    _intent(
        "race_ethnicity",
        r"\brac(?:e|ial)\b|\bethnic",
        "race racial ethnicity ethnic background identify describe origin group indicate select",
    ),
    _intent(
        "veteran",
        r"\bveteran",
        "veteran status protected military service identify",
    ),
    _intent(
        "disability",
        r"\bdisabilit|\bdisabled\b",
        "disability disabled status identify condition",
    ),
)

# Words that may surround a skill in "years of experience with X" and so do not
# name the skill.
_YEARS_GENERIC = frozenset(
    "years year experience many much total professional working hands professionally "
    "relevant overall".split()
)
_YEARS = re.compile(r"\byears?\b")


def _years_intent(text: str) -> str | None:
    if not _YEARS.search(text) or "experience" not in text and " of " not in f" {text} ":
        return None
    left = [w for w in _words(text) if w not in _STOP and w not in _NEGATIONS and w not in _YEARS_GENERIC]
    return f"years_of:{left[0]}" if len(left) == 1 else None


# Allowed wording for ``years_of:*`` intents.
_YEARS_ALLOWED = _YEARS_GENERIC


def _allowed_for(intent: str) -> frozenset[str]:
    if intent.startswith("years_of:"):
        return _COMMON_ALLOWED | _YEARS_ALLOWED | {intent.split(":", 1)[1]}
    if intent in ("sponsorship", "authorization"):
        return _COMMON_ALLOWED | _AUTH_ALLOWED
    return next(i.allowed for i in _INTENTS if i.name == intent)


_AUTH_ALLOWED = frozenset(
    "sponsorship sponsor visa immigration employment work working authorization authorized "
    "now future either currently current require requires need needs time any".split()
)


def intent_of(text) -> str | None:
    """The topic a question asks about, or ``None`` when unclear or compound.

    Authorization and sponsorship come from ``question_semantics`` so the two
    cannot be told apart differently here and there. Two topics in one text
    (a gender-and-race question) is no intent at all.
    """
    raw = str(text or "")
    cmp = comparable(raw)
    found = []
    kind = _auth_kind(raw)
    if kind == qs.AuthKind.SPONSORSHIP:
        found.append("sponsorship")
    elif kind == qs.AuthKind.AUTHORIZATION:
        found.append("authorization")
    elif kind == qs.AuthKind.AMBIGUOUS:
        return None
    found.extend(i.name for i in _INTENTS if i.pattern.search(raw))
    years = _years_intent(cmp)
    if years:
        found.append(years)
    return found[0] if len(found) == 1 else None


# --------------------------------------------------------------------------
# QM3-QM9: guards. Each returns a veto reason, or ``None`` to let the pair pass.
# --------------------------------------------------------------------------

_COUNTRY_ABBREV = re.compile(r"\b(?:USA|US|UK|UAE|U\.S\.A?\.?)\b")


def _countries(raw: str):
    # ``mentions.unresolved`` is deliberately ignored: "years of Python" reads
    # as an unrecognised place to that parser, and a real difference between
    # two unrecognised places is caught by the word-set rule for the strict
    # tiers and by the named-term rule below.
    mentions = qs.country_mentions(raw)
    abbreviations = frozenset(
        re.sub(r"\.", "", m).upper() for m in _COUNTRY_ABBREV.findall(raw)
    )
    return frozenset(mentions.alpha3), abbreviations


def _veto_typed_owned(raw_a, raw_b):
    if qs.blocks_llm(raw_a) or qs.blocks_llm(raw_b):
        return "typed_owned"
    return None


def _veto_polarity(words_a, words_b):
    if _NEGATIONS.intersection(words_a) != _NEGATIONS.intersection(words_b):
        return "polarity"
    return None


def _veto_auth_kind(raw_a, raw_b):
    ka, kb = _auth_kind(raw_a), _auth_kind(raw_b)
    if qs.AuthKind.AMBIGUOUS in (ka, kb) or ka != kb:
        return "auth_kind"
    return None


_NAMED = re.compile(r"^[A-Z][A-Za-z0-9+#.\-]*$")
# All-caps words that are ordinary vocabulary, not the name of a skill.
_PLAIN_ACRONYMS = frozenset("URL CV EEO EEOC ID PDF HR IT ASAP FAQ SSN PTO OK".split())


def _named_terms(raw: str) -> frozenset[str]:
    """Capitalised words after the first (names of skills, places, employers).

    The first word is skipped because every question starts with a capital.
    Not names: long ALL-CAPS words (shouting, "ETHNICITY"), slash-joined labels
    ("Race/Ethnicity"), and every word of a Title Case question (recognised by
    a capitalised function word such as "Is" or "Your").
    """
    tokens = [t.strip(".") for t in re.sub(r"[?!:;,()]", " ", raw).split()[1:]]
    if any(t.lower() in _STOP and t[:1].isupper() and t != "I" for t in tokens):
        return frozenset()
    named = [t for t in tokens if _NAMED.match(t) and t.lower() not in _STOP and not (t.isupper() and len(t) > 4) and t not in _PLAIN_ACRONYMS]
    return frozenset(t.lower() for t in named)


def _veto_named_terms(raw_a, raw_b, words_a, words_b):
    """A name in one question must appear somewhere in the other."""
    if _named_terms(raw_a) - set(words_b) or _named_terms(raw_b) - set(words_a):
        return "named_terms"
    return None


def _veto_countries(raw_a, raw_b):
    if _countries(raw_a) != _countries(raw_b):
        return "countries"
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """How one question compares with another.

    ``matched`` is True only when no guard vetoed the pair and it reached the
    threshold (or shares an intent). ``vetoed`` names the guard that stopped a
    pair, so a rejection can be explained in tests and reports.
    """

    matched: bool = False
    score: float = 0.0
    via: str = ""  # "words" | "intent" | "trigram" | ""
    intent: str | None = None
    vetoed: str | None = None


def match(a, b, *, min_score: float = DEFAULT_MIN_SCORE) -> Match:
    """Compare two question texts (QM3-QM11). Never raises on odd input."""
    raw_a, raw_b = str(a or ""), str(b or "")
    ca, cb = comparable(raw_a), comparable(raw_b)
    if not ca or not cb:
        return Match(vetoed="empty")
    words_a, words_b = _words(ca), _words(cb)
    score = similarity(ca, cb)

    for reason in (
        _veto_typed_owned(raw_a, raw_b),
        _veto_polarity(words_a, words_b),
        _veto_auth_kind(raw_a, raw_b),
        _veto_countries(raw_a, raw_b),
        _veto_named_terms(raw_a, raw_b, words_a, words_b),
    ):
        if reason:
            return Match(score=score, vetoed=reason)

    if _numbers(ca) != _numbers(cb):
        return Match(score=score, vetoed="numbers")

    intent_a, intent_b = intent_of(raw_a), intent_of(raw_b)
    if intent_a is not None and intent_b is not None and intent_a != intent_b:
        return Match(score=score, vetoed="intent")
    shared = intent_a if intent_a is not None and intent_a == intent_b else None

    tier_a, tier_b = classify_tier(raw_a), classify_tier(raw_b)
    if tier_a != tier_b:
        if shared is None:
            return Match(score=score, vetoed="tier")
        tier_a = higher_tier(tier_a, tier_b)
    content_a, content_b = _content(words_a), _content(words_b)

    if tier_a in _STRICT_TIERS:
        if content_a == content_b:
            return Match(True, score, "words", shared)
        if shared is not None and (content_a ^ content_b) <= _allowed_for(shared):
            return Match(True, score, "intent", shared)
        return Match(score=score, intent=shared, vetoed="strict_tier")

    if shared is not None:
        return Match(True, score, "intent", shared)
    if score >= min_score:
        return Match(True, score, "trigram")
    return Match(score=score)
