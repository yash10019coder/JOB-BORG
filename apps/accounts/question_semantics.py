"""What an application question is *asking*, decided without touching the DB.

A pure, deterministic leaf (like ``apps.accounts.tiering`` and
``apps.locations.engine``): regex rules plus the locations engine's country
lookup, no models, no network, no ``apps.auto_apply`` / ``apps.web`` imports.
``typed_facts`` uses it to decide which Profile fact (if any) answers a
question and to map a value onto the live form's options.

The guiding rule is the same as everywhere else in this effort: **a
confidently wrong answer is worse than none.** Every rule here either
recognises a question precisely or declines, and every ambiguity (two
countries, a negation, both sponsorship and authorization at once, a place we
cannot resolve) is reported as such so the caller leaves the field blank for
review instead of guessing.

None of these rules *write* an answer. They only say what kind of question
this is, which country it names, and which of the live options means yes/no.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from apps.accounts.tiering import QuestionCategory, classify
from apps.locations.engine import alpha3_for_country, country_records

# --------------------------------------------------------------------------
# Work authorization vs sponsorship
# --------------------------------------------------------------------------


class AuthKind(StrEnum):
    AUTHORIZATION = "authorization"
    SPONSORSHIP = "sponsorship"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


_SPONSORSHIP = re.compile(r"\bsponsor", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"\bwork authoriz|\bauthoriz(?:ed|ation) to work\b|"
    r"\beligib(?:le|ility) to work\b|\bright to work\b",
    re.IGNORECASE,
)
# A status the answer vocabulary cannot express as plain yes/no.
_AMBIGUOUS_STATUS = re.compile(
    r"\bh-?1b\b|\bimmigration\b|\bwork[ -]permit\b|\bcitizenship\b", re.IGNORECASE
)
# "Can you work without sponsorship?" flips the meaning of a yes/no, so the
# stored fact cannot be applied blindly.
_NEGATION = re.compile(
    r"\b(?:without|not|no longer|never)\b[^?.]{0,40}\b(?:sponsor|authoriz)", re.IGNORECASE
)
# Authorization that is qualified: an H-1B holder is authorized for their
# sponsor, not "without restriction" or "for any employer".
_RESTRICTION = re.compile(
    r"\b(?:without|no) (?:any )?restrictions?\b|\bany employer\b|\ball employers\b|"
    r"\bunrestricted\b",
    re.IGNORECASE,
)


def authorization_kind(text) -> AuthKind:
    """Which of authorization / sponsorship a question asks.

    Both "Are you authorized to work?" and "Will you require sponsorship?" are
    work-authorization questions, but "No" to one means the opposite of "No"
    to the other, so a stored answer for the wrong half must never be used.
    Ambiguous status questions, combined questions and negated phrasings are
    ``AMBIGUOUS`` and need a human.
    """
    text = text or ""
    sponsorship = _SPONSORSHIP.search(text)
    authorization = _AUTHORIZATION.search(text)
    if _AMBIGUOUS_STATUS.search(text) or _NEGATION.search(text) or (sponsorship and authorization):
        return AuthKind.AMBIGUOUS
    if sponsorship:
        return AuthKind.SPONSORSHIP
    if authorization:
        return AuthKind.AUTHORIZATION
    return AuthKind.NONE


def has_restriction_qualifier(text) -> bool:
    """True when authorization is asked "without restriction" / "any employer"."""
    return bool(_RESTRICTION.search(text or ""))


# --------------------------------------------------------------------------
# Countries named in a question
# --------------------------------------------------------------------------

# Short all-caps forms are accepted only from this list. Anything else that
# looks like an acronym ("IT", "HR", "KSA") is not assumed to be a country.
_ABBREVIATIONS = {"US": "USA", "USA": "USA", "UK": "GBR", "UAE": "ARE"}

# A country is named after one of these cue words, and starts with a capital.
# Case-sensitive on purpose: the engine resolves the lowercase words "us",
# "no", "it", "me", "be" and "is" to countries, so lowercase never counts.
_COUNTRY_CUE = re.compile(
    r"(?i:\b(?:in|within|inside|of|from))\s+(?i:the\s+)?(?=[A-Z])(?P<window>[^?.;:!\n]{1,80})"
)
_LIST_SEPARATOR = re.compile(r"\s*(?:,|/|&|\band\b|\bor\b)\s*")
_DOTTED_USA = re.compile(r"\bU\.S\.A\.?")
_DOTTED_US = re.compile(r"\bU\.S\.?")
# Word characters on purpose: a placeholder containing a bare "and" would be
# split by the list separator all over again.
_AND = "_AND_"

# Country names that contain "and" ("Trinidad and Tobago") must not be split
# like a list.
_AND_NAMES = sorted(
    (r["label"] for r in country_records() if " and " in r["label"]), key=len, reverse=True
)


@dataclass(frozen=True)
class CountryMentions:
    """Countries a question names.

    ``alpha3`` holds every country that resolved. ``unresolved`` is True when
    something country-shaped was named but could not be resolved (a state, a
    city, an acronym) -- the caller must not then fall back to "the job's
    country", because the question is about somewhere else.
    """

    alpha3: tuple[str, ...] = ()
    unresolved: bool = False

    @property
    def single(self) -> str | None:
        return self.alpha3[0] if len(self.alpha3) == 1 and not self.unresolved else None

    @property
    def ambiguous(self) -> bool:
        return len(self.alpha3) > 1


def _resolve_token(token: str) -> tuple[str | None, bool]:
    """``(alpha3, stopped)`` for one list item; trims words from the right so
    "United States for our Company" resolves to the United States."""
    token = re.sub(r"^(?i:the)\s+", "", token.strip())
    words = token.split()
    for end in range(len(words), 0, -1):
        candidate = " ".join(words[:end]).strip(" .")
        if not candidate or not candidate[0].isupper():
            return None, True
        if candidate in _ABBREVIATIONS:
            return _ABBREVIATIONS[candidate], False
        if candidate.isupper() and len(candidate) <= 3:
            return None, False  # an unlisted acronym: not a country we know
        alpha3 = alpha3_for_country(candidate)
        if alpha3:
            return alpha3, False
    return None, False


def country_mentions(text) -> CountryMentions:
    """Countries named in ``text``, found only after "in/within/of/from"."""
    text = _DOTTED_US.sub("US", _DOTTED_USA.sub("USA", text or ""))
    found: list[str] = []
    unresolved = False
    for match in _COUNTRY_CUE.finditer(text):
        window = match.group("window")
        for name in _AND_NAMES:
            window = window.replace(name, name.replace(" and ", _AND))
        for index, token in enumerate(_LIST_SEPARATOR.split(window)):
            token = token.replace(_AND, " and ").strip()
            if not token:
                continue
            alpha3, stopped = _resolve_token(token)
            if stopped and index > 0:
                break  # prose after the list, not another place
            if alpha3 is None:
                unresolved = True
            elif alpha3 not in found:
                found.append(alpha3)
    return CountryMentions(tuple(found), unresolved)


# --------------------------------------------------------------------------
# Citizenship
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CitizenshipQuestion:
    kind: str  # "is_citizen" ("Are you a citizen of X?") or "nationality"
    countries: tuple[str, ...] = ()
    unresolved: bool = False


# Questions about a status or a permit belong to the authorization rules.
_NOT_CITIZENSHIP = re.compile(
    r"\bcitizenship status\b|\bimmigration\b|\bwork[ -]permit\b|\bh-?1b\b|\bvisa\b|"
    r"\bsponsor|\bauthoriz|\beligib",
    re.IGNORECASE,
)
_IS_CITIZEN = re.compile(
    r"\bare you (?:currently )?(?:a |an )?(?:[\w.'-]+\s+){0,2}?(?:citizen|national)\b",
    re.IGNORECASE,
)
_DEMONYM_CITIZEN = re.compile(
    r"\b(?:a|an)\s+(?P<c>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)?)\s+(?:citizen|national)\b"
)
_NATIONALITY = re.compile(
    r"^\s*(?:your\s+)?(?:nationality|citizenship)\s*\*?:?\s*$|"
    r"\b(?:your|current|indicate|select|enter|provide)\b[^?.]{0,20}\b(?:nationality|country of citizenship)\b|"
    r"\bcountry of citizenship\b",
    re.IGNORECASE,
)


def citizenship_question(text) -> CitizenshipQuestion | None:
    """Recognise "Are you a citizen of X?" and nationality/citizenship selects."""
    text = text or ""
    if _NOT_CITIZENSHIP.search(text):
        return None
    if _IS_CITIZEN.search(text):
        mentions = country_mentions(text)
        countries = list(mentions.alpha3)
        unresolved = mentions.unresolved
        demonym = _DEMONYM_CITIZEN.search(_DOTTED_US.sub("US", _DOTTED_USA.sub("USA", text)))
        if demonym:
            alpha3, _ = _resolve_token(demonym.group("c"))
            if alpha3 is None:
                unresolved = True
            elif alpha3 not in countries:
                countries.append(alpha3)
        return CitizenshipQuestion("is_citizen", tuple(countries), unresolved)
    if _NATIONALITY.search(text):
        return CitizenshipQuestion("nationality")
    return None


# --------------------------------------------------------------------------
# Contact and location questions
# --------------------------------------------------------------------------


class ContactKind(StrEnum):
    COUNTRY = "country"
    CITY = "city"
    LOCATION = "location"
    ADDRESS = "address"
    TIMEZONE = "timezone"
    MEMBERSHIP = "membership"  # "Are you located in X?"


_CONTACT_EXCLUDED = re.compile(
    r"\brelocat|\bwilling\b|\bcommut|\bapplying for\b|\boffice\b|\bprefer", re.IGNORECASE
)
_COUNTRY_Q = re.compile(
    r"^\s*(?:current\s+)?country(?:\s+of residence)?\s*\*?:?\s*$|"
    r"\bcountry of residence\b|"
    r"\b(?:what|which) country\b.*\b(?:located|based|live|living|reside|residing|work|working)\b|"
    r"\bin which country\b.*\b(?:located|based|live|living|reside|residing|work|working)\b",
    re.IGNORECASE,
)
_CITY_Q = re.compile(
    r"^\s*(?:current\s+)?(?:location\s*\(city\)|city(?:\s+of residence)?)\s*\*?:?\s*$",
    re.IGNORECASE,
)
_ADDRESS_Q = re.compile(
    r"^\s*(?:home |mailing |current |full |street )?address\s*\*?:?\s*$|"
    r"\b(?:provide|enter|what(?:'s| is)) your (?:home |mailing |current |full )?address\b",
    re.IGNORECASE,
)
_TIMEZONE_Q = re.compile(r"\btime ?zone\b", re.IGNORECASE)
_LOCATION_Q = re.compile(
    r"^\s*(?:current\s+)?location\s*\*?:?\s*$|"
    r"\bwhat(?:'s| is) your (?:current )?location\b|"
    r"\bwhere are you (?:currently )?(?:located|based)\b|"
    r"\bwhere do you (?:currently )?(?:live|reside)\b|"
    r"\bwhat location are you based in\b",
    re.IGNORECASE,
)
# Anchored: "Where are you based?" asks for a location, it does not test one.
_MEMBERSHIP_Q = re.compile(
    r"^\s*are you (?:currently )?(?:located|based|living|residing)\b", re.IGNORECASE
)


def contact_kind(text) -> ContactKind | None:
    """Which contact/location fact a question asks for, or ``None``."""
    text = text or ""
    if _CONTACT_EXCLUDED.search(text):
        return None
    if _ADDRESS_Q.search(text):
        return ContactKind.ADDRESS
    if _TIMEZONE_Q.search(text):
        return ContactKind.TIMEZONE
    if _CITY_Q.search(text):
        return ContactKind.CITY
    if _COUNTRY_Q.search(text):
        return ContactKind.COUNTRY
    if _MEMBERSHIP_Q.search(text):
        return ContactKind.MEMBERSHIP
    if _LOCATION_Q.search(text):
        return ContactKind.LOCATION
    return None


def blocks_llm(text) -> bool:
    """Questions the typed layer owns outright: citizenship and "are you
    located in X". If the profile cannot answer one confidently it stays blank
    for review -- an LLM guessing a citizenship or residence from a resume is
    exactly the confident wrong answer to avoid."""
    return citizenship_question(text) is not None or contact_kind(text) == ContactKind.MEMBERSHIP


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------

_CURRENT_SALARY = re.compile(
    r"\b(?:current|present|previous|last)\b[^?.]{0,20}\b(?:salary|compensation|pay)\b",
    re.IGNORECASE,
)
_YES_NO_LEAD = re.compile(r"^\s*(?:are|is|do|does|would|will|can)\b", re.IGNORECASE)


def is_salary_expectation(text) -> bool:
    """A free-form "what do you expect to be paid" question.

    Excludes what the applicant earns *now* or earned before, and yes/no
    phrasings ("Are you comfortable with $100k?"), which a salary band cannot
    answer.
    """
    text = text or ""
    if classify(text) != QuestionCategory.SALARY_EXPECTATION:
        return False
    return not (_CURRENT_SALARY.search(text) or _YES_NO_LEAD.search(text))


# --------------------------------------------------------------------------
# Location sensitivity
# --------------------------------------------------------------------------

_LOCATION_SENSITIVE = re.compile(
    r"\brelocat|\bon-?site\b|\bin[- ]office\b|\bhybrid\b|\bcommut|"
    r"\bwork(?:ing)? from (?:the )?office\b",
    re.IGNORECASE,
)


def is_location_sensitive(text) -> bool:
    """Whether the right answer depends on *where the job is* without saying so.

    Relocation, on-site/hybrid and commuting wording. A country named in the
    question does not count: "authorized to work in the United States" already
    carries its country in its own key, so it cannot be reused for a job
    elsewhere.
    """
    return bool(_LOCATION_SENSITIVE.search(text or ""))


# --------------------------------------------------------------------------
# Option pickers
# --------------------------------------------------------------------------


def _normalize_option(option) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(option).lower()).strip()


def pick_exact(options, value) -> str | None:
    """The single option equal to ``value`` ignoring case/punctuation."""
    wanted = _normalize_option(value)
    if not wanted:
        return None
    matches = [o for o in options if _normalize_option(o) == wanted]
    return matches[0] if len(matches) == 1 else None


def pick_yes_no(options, truth: bool) -> str | None:
    """The one option that means yes (or no) on this form.

    With no options (free text) the answer is the literal "Yes"/"No". With
    options, exactly one must be a yes (or no) -- "Yes" / "Yes, I do" -- and
    anything ambiguous ("No, I require sponsorship" next to "No, but I do not
    require sponsorship") returns ``None`` rather than guessing.
    """
    if not options:
        return "Yes" if truth else "No"
    lead = "yes" if truth else "no"
    matches = [o for o in options if (_normalize_option(o).split() or [""])[0] == lead]
    return matches[0] if len(matches) == 1 else None


_DIAL_SUFFIX = re.compile(r"\s*\+\d[\d\s-]*$")
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


def option_dial_code(option) -> str | None:
    """Digits of a trailing ``+NN`` dial code ("India +91" -> "91")."""
    match = re.search(r"\+(\d[\d\s-]*)$", str(option))
    return re.sub(r"\D", "", match.group(1)) if match else None


def pick_country_option(options, alpha3: str) -> str | None:
    """The one option naming country ``alpha3``.

    Options may carry a phone dial code ("India +91") or a parenthetical;
    both are stripped before comparing by alpha-3, so "British Indian Ocean
    Territory +246" can never be mistaken for India.
    """
    matches = []
    for option in options:
        name = _PARENTHETICAL.sub("", _DIAL_SUFFIX.sub("", str(option))).strip()
        if name and alpha3_for_country(name) == alpha3:
            matches.append(option)
    return matches[0] if len(matches) == 1 else None
