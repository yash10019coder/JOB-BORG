"""Source precedence shared by every writer of provenance-carrying data.

user-locked > user-set > learned > imported (FR7.3). Kept in one place so
``AnswerBank`` writes (``answer_resolver``) and Profile column writes
(``profile_fields``) can never disagree about who outranks whom.
"""
from apps.accounts.models import AnswerBank

SOURCE_RANK = {
    AnswerBank.Source.IMPORTED: 1,
    AnswerBank.Source.LEARNED: 2,
    AnswerBank.Source.USER: 3,
}
LOCKED_RANK = 4


def is_blank(value):
    """True for values that carry no information (``None``, ``""``, ``[]``, ``{}``)."""
    return value in (None, "", [], {})
