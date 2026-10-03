"""Question tiering: category classifier plus the T0/T1/T2 answer-risk tier.

Dependency-free leaf (same posture as ``apps/locations/engine.py``): plain
regex matching, no DB, no network, no imports from other apps. It lives in
``apps.accounts`` so the answer resolver can use it without importing
``apps.auto_apply`` (which already imports ``apps.accounts``);
``apps/auto_apply/llm/categories.py`` re-exports the category API.

The category classifier is a hard security/trust boundary that runs *before*
any LLM inference -- see ``HARD_EXCLUDED_CATEGORIES`` and
``apps.auto_apply.llm.base.resolve_answers``. The tier layer below it decides
whether a non-user-sourced answer may be submitted without the user's explicit
confirmation (see docs/plans/2026-10-03-2300-consolidated-requirements.md).
"""
import re


class QuestionCategory:
    """Category labels a rendered application question can be tagged with."""

    WORK_AUTHORIZATION = "work_authorization"
    LEGAL_ATTESTATION = "legal_attestation"
    BACKGROUND_CHECK = "background_check"
    SALARY_EXPECTATION = "salary_expectation"
    DEMOGRAPHIC = "demographic"
    GENERIC = "generic"


# Categories that are ALWAYS routed to "requires explicit human answer",
# regardless of LLM confidence -- infer() must never be called for a question
# tagged with one of these. This is a hard boundary, not a soft preference:
# work-authorization/sponsorship answers, legally-binding attestations,
# background-check disclosures, and salary figures are the kinds of answers
# where a wrong or hallucinated LLM guess carries outsized real-world risk
# (a false attestation, a misstated visa status, an unauthorized salary
# commitment).
HARD_EXCLUDED_CATEGORIES = frozenset(
    {
        QuestionCategory.WORK_AUTHORIZATION,
        QuestionCategory.LEGAL_ATTESTATION,
        QuestionCategory.BACKGROUND_CHECK,
        QuestionCategory.SALARY_EXPECTATION,
        QuestionCategory.DEMOGRAPHIC,
    }
)

# Ordered (category, patterns) pairs -- first match wins. Patterns are
# case-insensitive regexes matched against the rendered question text.
_CATEGORY_PATTERNS = [
    (
        QuestionCategory.DEMOGRAPHIC,
        [
            r"\b(gender|sex|race|racial|ethnicity|ethnic|demographic)\b",
            r"\b(veteran|disability|disabled|sexual orientation|pronouns)\b",
            r"\b(hispanic|latino|latina|latinx)\b",
        ],
    ),
    (
        QuestionCategory.WORK_AUTHORIZATION,
        [
            r"\bwork authoriz",
            r"\bauthoriz(ed|ation) to work\b",
            r"\beligib(le|ility) to work\b",
            r"\bsponsor(ship)?\b",
            r"\bvisa\b",
            r"\bh-?1b\b",
            r"\bwork permit\b",
            r"\bright to work\b",
            r"\bcitizenship status\b",
            r"\bimmigration status\b",
        ],
    ),
    (
        QuestionCategory.LEGAL_ATTESTATION,
        [
            r"\bunder penalty of perjury\b",
            r"\bi certify (that|the)\b",
            r"\bi attest\b",
            r"\bi hereby (certify|attest|declare)\b",
            r"\bnon-?compete\b",
            r"\blegally binding\b",
            r"\be-?signature\b",
            r"\bi agree to the terms\b",
        ],
    ),
    (
        QuestionCategory.BACKGROUND_CHECK,
        [
            r"\bbackground check\b",
            r"\bcriminal (history|record)\b",
            r"\bever (been )?convicted\b",
            r"\bfelony\b",
            r"\bmisdemeanor\b",
            r"\bdrug test\b",
            r"\bconsent to a background\b",
        ],
    ),
    (
        QuestionCategory.SALARY_EXPECTATION,
        [
            r"\bsalary expectat",
            r"\bcompensation expectat",
            r"\bdesired (salary|compensation|pay)\b",
            r"\b(salary|compensation|pay) requirements?\b",
            r"\bexpected (pay|salary|compensation)\b",
            r"\bcurrent[\w\s]{0,20}salary\b",
            r"\bcurrent[\w\s]{0,20}compensation\b",
            r"\bpay range\b",
            r"\bpay expectat",
            r"\btarget compensation\b",
            r"\bsalary\b",
            r"\bcompensation (range|requirement|expectation)s?\b",
        ],
    ),
    (
        # Voluntary self-identification / EEO questions: an LLM must never
        # infer these from a resume or profile.
        QuestionCategory.DEMOGRAPHIC,
        [
            r"\bgender\b",
            r"\bsex\b",
            r"\breligio(n|us)\b",
            r"\blgbt[qia]*\b",
            r"\bnational origin\b",
            r"\bprotected[ -]class\b",
            r"\bage\b",
            r"\bhow old\b",
            r"\b\d{1,3} years? old\b",
            r"\bpronouns?\b",
            r"\btransgender\b",
            r"\bsexual orientation\b",
            r"\brace\b",
            r"\bethnic(ity)?\b",
            r"\bhispanic\b",
            r"\blatino\b",
            r"\bveteran\b",
            r"\bdisabilit",
            r"\bmarital status\b",
            r"\bdate of birth\b",
        ],
    ),
]


def classify(question_text):
    """Return the ``QuestionCategory`` for a rendered question string.

    Falls back to ``QuestionCategory.GENERIC`` (an allowed category eligible
    for LLM inference) when nothing matches -- classification is only used
    to *exclude* sensitive categories, not to whitelist specific allowed
    phrasings.
    """
    text = (question_text or "").lower()
    for category, patterns in _CATEGORY_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            return category
    return QuestionCategory.GENERIC


# --- Answer-risk tiers -----------------------------------------------------


class Tier:
    """Answer-risk tiers. Values mirror ``accounts.AnswerBank.RiskTier``."""

    T0_LEGAL = "t0_legal"
    T1_COMMERCIAL = "t1_commercial"
    T2_FACTUAL = "t2_factual"


_TIER_RANK = {Tier.T2_FACTUAL: 0, Tier.T1_COMMERCIAL: 1, Tier.T0_LEGAL: 2}

# Version stamped into submit snapshots so a later classifier change is
# attributable. Bump whenever the tier logic or patterns change.
TIERING_VERSION = "v1"


def higher_tier(a, b):
    """Return the riskier of two tiers (T0 > T1 > T2)."""
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


_CATEGORY_TIER = {
    QuestionCategory.WORK_AUTHORIZATION: Tier.T0_LEGAL,
    QuestionCategory.LEGAL_ATTESTATION: Tier.T0_LEGAL,
    QuestionCategory.BACKGROUND_CHECK: Tier.T0_LEGAL,
    QuestionCategory.DEMOGRAPHIC: Tier.T0_LEGAL,
    QuestionCategory.SALARY_EXPECTATION: Tier.T1_COMMERCIAL,
}

# Patterns beyond the category list, so the tier layer can only ever be
# *stricter* than the category classifier.
_EXTRA_T0_PATTERNS = [
    r"\bclearance\b",
    r"\bcitizen(ship)?\b",
    r"\bemployed by\b",
    r"\b(previously|formerly) (employed|worked)\b",
    r"\bworked (for|at) .{0,40}\b(before|previously)\b",
    r"\b(post-?employment|contractual|restrictive) (restrictions?|covenants?)\b",
    r"\bnon-?solicit",
    r"\bconflicts? of interest\b",
    r"\bconsent\b",
    r"\backnowledg",
    r"\bprivacy (policy|notice)\b",
    r"\bdisclosure\b",
    r"\bagree (to|with)\b",
]

_T1_PATTERNS = [
    r"\bnotice period\b",
    r"\bstart date\b",
    r"\b(when|how soon) (can|could|are) you (start|available)\b",
    r"\bavailab(le|ility) to start\b",
    r"\brelocat",
    r"\bwilling(ness)? to (move|travel)\b",
    r"\bpay\b",
    r"\bcompensation\b",
    r"\bhourly rate\b",
]

# T2 requires a *positive* match: an unrecognised question is never assumed
# harmless.
_T2_PATTERNS = [
    r"\b(phone|mobile|telephone)\b",
    r"\baddress\b",
    r"\b(city|town)\b",
    r"\b(postal|zip) ?code\b",
    r"^\s*(state|province|country|region)\s*\*?\s*$",
    r"\btime ?zone\b",
    r"\blocation\b",
    r"\bresid(e|ence)\b",
    r"\bbased\b",
    r"\bhow did you (hear|find)\b",
    r"\bhear about\b",
    r"\brefer(red|ral)?\b",
    r"\b(github|gitlab|linkedin|portfolio|website|personal site)\b",
    r"\bexperience\b",
    r"\byears?\b.{0,40}\b(of|with|in|using)\b",
    r"\bhave you (used|sold|worked with)\b",
    r"\b(current user of|used in the past)\b",
    r"\b(fluent|proficien\w*|language)\b",
    r"\b(event|conference)\b",
    r"\b(skills?|technolog\w*|degree|university|education)\b",
    r"\b(cover letter|resume|cv)\b",
]


def _matches(patterns, text):
    return any(re.search(pattern, text) for pattern in patterns)


def classify_tier(question_text):
    """Return the ``Tier`` for a rendered question string.

    The riskiest signal wins: every category and every extra T0/T1 pattern is
    checked (not just the first category match), so a compound question such
    as "5 years of experience and are you authorized to work?" is T0. T2
    needs a positive allowlist match; anything unrecognised -- including an
    empty string -- is T0.
    """
    text = (question_text or "").lower()
    tier = None
    for category, patterns in _CATEGORY_PATTERNS:
        if _matches(patterns, text):
            candidate = _CATEGORY_TIER[category]
            tier = candidate if tier is None else higher_tier(tier, candidate)
    if _matches(_EXTRA_T0_PATTERNS, text):
        return Tier.T0_LEGAL
    if tier == Tier.T0_LEGAL:
        return tier
    if _matches(_T1_PATTERNS, text):
        return Tier.T1_COMMERCIAL
    if tier is not None:
        return tier
    if _matches(_T2_PATTERNS, text):
        return Tier.T2_FACTUAL
    return Tier.T0_LEGAL
