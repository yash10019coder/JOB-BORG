"""Shadow precision of the consensus learner (FR8.6), measured, not enforced.

For every observation where a learned answer was prefilled, compare it with
what was finally submitted. ``precision`` is the share submitted unchanged: the
accuracy a future auto-apply would have had. The Wilson lower bound is the
conservative figure the Phase 5 gate will compare to its threshold. Nothing
here changes behaviour.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import sqrt

from apps.accounts.models import AnswerObservation
from apps.accounts.services.learning import _fold

BULK_ORIGIN = "draft_review_bulk"


@dataclass
class KeyStats:
    question_key: str
    observations: int = 0
    matched: int = 0
    bulk_confirmed: int = 0

    @property
    def precision(self):
        return self.matched / self.observations if self.observations else None

    @property
    def lower_bound(self):
        return wilson_lower_bound(self.matched, self.observations)


def wilson_lower_bound(successes, n, z=1.96):
    """Lower end of the Wilson score interval; 0.0 with no data."""
    if n == 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denominator)


def shadow_stats(profile=None):
    """``{question_key: KeyStats}`` over observations that had a learned prefill."""
    observations = AnswerObservation.objects.filter(learned_value__isnull=False)
    if profile is not None:
        observations = observations.filter(profile=profile)
    stats = defaultdict(lambda: KeyStats(""))
    for obs in observations.only("question_key", "value", "learned_value", "provenance_origin"):
        entry = stats[obs.question_key]
        entry.question_key = obs.question_key
        entry.observations += 1
        entry.matched += _fold(obs.value) == _fold(obs.learned_value)
        entry.bulk_confirmed += obs.provenance_origin == BULK_ORIGIN
    return dict(stats)
