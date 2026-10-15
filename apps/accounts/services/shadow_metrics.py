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

from apps.accounts.models import AnswerBank, AnswerObservation
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


@dataclass
class CandidateStats:
    """How a reworded-question candidate fared, per risk tier.

    ``precision`` counts only answers the user typed or changed (a click on
    "use this answer" is not evidence the match was right), and leaves out
    candidates drawn from an answer that was itself saved through an accept, so
    accepted answers cannot raise their own score. ``accept_rate`` is reported
    separately: a high rate with no typed answers means the number is
    uninformative, not good.
    """

    tier: str
    shown: int = 0
    accepted: int = 0
    typed: int = 0
    matched: int = 0
    self_reinforced: int = 0

    @property
    def precision(self):
        return self.matched / self.typed if self.typed else None

    @property
    def lower_bound(self):
        return wilson_lower_bound(self.matched, self.typed)

    @property
    def accept_rate(self):
        return self.accepted / self.shown if self.shown else None


def candidate_stats(profile=None):
    """``{tier: CandidateStats}`` over observations where a candidate was shown."""
    observations = AnswerObservation.objects.exclude(candidate_question_key="")
    if profile is not None:
        observations = observations.filter(profile=profile)
    reinforced = set(
        AnswerBank.objects.filter(source_detail__via_candidate=True)
        .values_list("profile_id", "question_key")
    )
    stats = {}
    for obs in observations.only(
        "profile_id", "tier", "candidate_question_key", "candidate_equal", "candidate_accepted"
    ):
        entry = stats.setdefault(obs.tier, CandidateStats(obs.tier))
        entry.shown += 1
        if obs.candidate_accepted:
            entry.accepted += 1
            continue
        if (obs.profile_id, obs.candidate_question_key) in reinforced:
            entry.self_reinforced += 1
            continue
        entry.typed += 1
        entry.matched += bool(obs.candidate_equal)
    return stats


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
