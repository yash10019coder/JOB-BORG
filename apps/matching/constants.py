"""Matching constants — the single source of truth for the recommend cutoff.

``match_status`` is derived by comparing ``match_score`` against
``MATCH_SCORE_THRESHOLD`` — the recommendation view (U12) filters on the status,
never on a raw score, so this constant is the only place the boundary lives.
"""

# Scores are in [0.0, 1.0]. A job at or above this is worth showing.
MATCH_SCORE_THRESHOLD = 0.40


class MatchStatus:
    RECOMMENDED = "recommended"
    BELOW_THRESHOLD = "below_threshold"

    CHOICES = [
        (RECOMMENDED, "Recommended"),
        (BELOW_THRESHOLD, "Below threshold"),
    ]


# Weighted scoring components (weights sum to 1.0).
TAG_WEIGHT = 0.50
TITLE_WEIGHT = 0.20
LOCATION_WEIGHT = 0.20
SALARY_WEIGHT = 0.10


def status_for_score(score):
    return (
        MatchStatus.RECOMMENDED
        if score >= MATCH_SCORE_THRESHOLD
        else MatchStatus.BELOW_THRESHOLD
    )
