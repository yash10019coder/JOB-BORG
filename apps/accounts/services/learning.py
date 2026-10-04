"""Consensus learner: reuse what the user keeps answering the same way.

Reads ``AnswerObservation`` (what the user actually submitted) and, when the
same answer to the same question was authored on the most recent distinct
employers, writes it as a ``source=learned`` ``AnswerBank`` row so the next
application is prefilled. A learned answer is only ever a *prefill*: the
resolver holds it for explicit confirmation in every tier, so nothing learned
is submitted unreviewed (NFR2).

Evidence is what the user *authored*. An observation counts only when the user
typed or changed the value (``user_edited``) and did not untick the remember
box (``remember_declined``). A re-posted LLM guess, a typed-settings value, or
a learned prefill the user merely confirmed is neutral -- otherwise the learner
would confirm its own output. Fail closed: disagreement or too little evidence
writes nothing, and a newest answer that contradicts a learned row withdraws it.

``apps.accounts`` must not import ``apps.auto_apply`` or ``apps.web``; this
module only needs the models, the resolver and the question semantics.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts import question_semantics as qs
from apps.accounts.models import AnswerBank, AnswerObservation, ProfileSuggestion
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import (
    delete_answer,
    load_bank_rows,
    would_refuse,
    write_answer,
)
from apps.accounts.tiering import Tier, classify_tier, higher_tier

logger = logging.getLogger(__name__)

# Bump when the consensus rules change, so rows and suggestions can be
# re-derived (mirrors CURRENT_LOCATION_ALIAS_VERSION in apps.locations).
LEARNER_VERSION = "v1"

# Narrative and file answers are about one employer / one upload.
_NOT_LEARNABLE_FIELD_TYPES = ("textarea", "file")
_HELD_TIERS = (Tier.T0_LEGAL, Tier.T1_COMMERCIAL)
_MAX_CONFIDENCE = 0.95


def min_distinct_employers():
    return max(2, int(getattr(settings, "LEARNING_MIN_DISTINCT_EMPLOYERS", 2)))


def suggestion_ttl():
    return timedelta(days=int(getattr(settings, "LEARNING_SUGGESTION_TTL_DAYS", 90)))


@dataclass
class LearnReport:
    dry_run: bool = False
    disabled: bool = False
    written: list = field(default_factory=list)
    withdrawn: list = field(default_factory=list)
    suggested: list = field(default_factory=list)
    expired: int = 0
    skipped: dict = field(default_factory=dict)

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def changed(self):
        return bool(self.written or self.withdrawn or self.suggested or self.expired)


def _fold(value):
    """Comparison form of an answer: case/whitespace-insensitive, lists unordered."""
    if isinstance(value, (list, tuple)):
        return tuple(sorted(_fold(item) for item in value))
    return " ".join(str(value).casefold().split())


def fingerprint(value):
    return hashlib.sha1(json.dumps(_fold(value), ensure_ascii=False).encode()).hexdigest()


def _employer_identity(obs):
    name = (obs.employer_name or "").strip().casefold()
    return name or f"job:{obs.job_id}"


def _eligible_observations(profile):
    return (
        AnswerObservation.objects.filter(
            profile=profile,
            user_edited=True,
            remember_declined=False,
            job_id__isnull=False,
        )
        .exclude(field_type__in=_NOT_LEARNABLE_FIELD_TYPES)
        .order_by("-created_at", "-id")
    )


def _groups(profile, report):
    """``{(question_key, scope): [newest observation per employer, newest first]}``."""
    groups = {}
    seen = {}
    for obs in _eligible_observations(profile):
        scope = ""
        if qs.is_location_sensitive(obs.question_text):
            scope = obs.job_region
            if not scope:
                report.skip("location_sensitive_without_region")
                continue
        key = (obs.question_key, scope)
        employer = _employer_identity(obs)
        if (key, employer) in seen:
            continue
        seen[(key, employer)] = True
        groups.setdefault(key, []).append(obs)
    return groups


def _consensus(entries):
    """``(newest observation, agreeing employers)`` or ``(None, reason)``."""
    needed = min_distinct_employers()
    if len(entries) < needed:
        return None, "not_enough_evidence"
    window = entries[:needed]
    if len({_fold(o.value) for o in window}) != 1:
        return None, "disagreement"
    agreeing = 0
    for obs in entries:
        if _fold(obs.value) != _fold(window[0].value):
            break
        agreeing += 1
    return window[0], agreeing


def _expire_stale_suggestions(profile, now, dry_run):
    stale = ProfileSuggestion.objects.filter(
        profile=profile,
        status=ProfileSuggestion.Status.PENDING,
        created_at__lt=now - suggestion_ttl(),
    )
    count = stale.count()
    if count and not dry_run:
        stale.update(status=ProfileSuggestion.Status.EXPIRED, resolved_at=now, updated_at=now)
    return count


def _ensure_suggestion(profile, newest, scope, tier, evidence, confidence, dry_run):
    """Offer to promote a learned legal/commercial answer. Returns True if new."""
    existing = ProfileSuggestion.objects.filter(
        profile=profile,
        question_key=newest.question_key,
        scope_region=scope,
        value_fingerprint=fingerprint(newest.value),
    ).first()
    if existing is not None:
        if existing.status == ProfileSuggestion.Status.EXPIRED and not dry_run:
            existing.status = ProfileSuggestion.Status.PENDING
            existing.resolved_at = None
            existing.save(update_fields=["status", "resolved_at", "updated_at"])
        return False
    if not dry_run:
        ProfileSuggestion.objects.create(
            profile=profile,
            question_key=newest.question_key,
            scope_region=scope,
            question_text=newest.question_text,
            value=newest.value,
            value_fingerprint=fingerprint(newest.value),
            tier=tier,
            evidence=evidence,
            confidence=confidence,
            learner_version=LEARNER_VERSION,
        )
    return True


def _suppressed(profile, newest, scope):
    return ProfileSuggestion.objects.filter(
        profile=profile,
        question_key=newest.question_key,
        scope_region=scope,
        value_fingerprint=fingerprint(newest.value),
        status=ProfileSuggestion.Status.REJECTED,
    ).exists()


def learn_for_profile(profile, *, dry_run=False, now=None):
    """Learn from the profile's observations. Idempotent; safe to rerun."""
    now = now or timezone.now()
    report = LearnReport(dry_run=dry_run)
    if not profile.learning_enabled:
        report.disabled = True
        return report

    report.expired = _expire_stale_suggestions(profile, now, dry_run)
    bank_rows = load_bank_rows(profile)

    for (key, scope), entries in _groups(profile, report).items():
        newest = entries[0]
        existing = bank_rows.get((key, scope))
        learned_existing = (
            existing if existing is not None and existing.source == AnswerBank.Source.LEARNED else None
        )

        if typed_facts.resolve(profile, newest.question_text) is not None:
            report.skip("typed_setting_owns_it")
            continue

        winner, outcome = _consensus(entries)
        if winner is None:
            # The newest answer contradicts what was learned: stop prefilling it.
            if learned_existing is not None and _fold(newest.value) != _fold(learned_existing.value):
                if not dry_run:
                    delete_answer(learned_existing, deleted_by="learner_withdraw")
                report.withdrawn.append(key)
            else:
                report.skip(outcome)
            continue

        if learned_existing is not None and _fold(learned_existing.value) == _fold(winner.value):
            report.skip("already_learned")
            continue
        if _suppressed(profile, winner, scope):
            report.skip("suppressed_by_user")
            continue
        if would_refuse(existing, AnswerBank.Source.LEARNED, now):
            report.skip("user_answer_exists")
            continue

        tier = higher_tier(winner.tier, classify_tier(winner.question_text))
        confidence = min(_MAX_CONFIDENCE, 0.6 + 0.1 * outcome)
        evidence = {
            "employers": [o.employer_name for o in entries[:outcome]],
            "job_ids": [o.job_id for o in entries[:outcome]],
            "count": outcome,
        }
        if not dry_run:
            result = write_answer(
                profile,
                winner.question_text,
                winner.value,
                AnswerBank.Source.LEARNED,
                question_key=key,
                scope_region=scope,
                confidence=confidence,
                min_tier=tier,
                source_detail={
                    "origin": "consensus",
                    "learner_version": LEARNER_VERSION,
                    **evidence,
                },
            )
            if not result.applied:
                report.skip("user_answer_exists")
                continue
        report.written.append(key)
        if tier in _HELD_TIERS and _ensure_suggestion(
            profile, winner, scope, tier, evidence, confidence, dry_run
        ):
            report.suggested.append(key)
    return report
