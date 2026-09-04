"""Rule-based question-category classifier.

Mirrors ``apps/classification/rule_types.py``'s posture: plain regex/keyword
matching, no I/O, no per-question LLM calls. This classifier runs *before*
any LLM inference is attempted and is the hard security/trust boundary that
keeps sensitive questions from ever reaching the LLM client -- see
``HARD_EXCLUDED_CATEGORIES`` and ``base.resolve_answers``.
"""
import re


class QuestionCategory:
    """Category labels a rendered application question can be tagged with."""

    WORK_AUTHORIZATION = "work_authorization"
    LEGAL_ATTESTATION = "legal_attestation"
    BACKGROUND_CHECK = "background_check"
    SALARY_EXPECTATION = "salary_expectation"
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
    }
)

# Ordered (category, patterns) pairs -- first match wins. Patterns are
# case-insensitive regexes matched against the rendered question text.
_CATEGORY_PATTERNS = [
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
            r"\bdesired salary\b",
            r"\bexpected (pay|salary|compensation)\b",
            r"\bcurrent[\w\s]{0,20}salary\b",
            r"\bcurrent[\w\s]{0,20}compensation\b",
            r"\bpay range\b",
            r"\bpay expectat",
            r"\btarget compensation\b",
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
