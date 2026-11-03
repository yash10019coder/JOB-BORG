"""The Learning tab: what the consensus learner picked up, and what to do with it.

Every row is looked up through ``request.user.profile`` (404 otherwise), and
every write goes through ``write_answer`` / ``delete_answer`` so precedence and
history behave exactly as on the Answers page. Nothing here edits the Profile
columns that matching reads, so none of it can trigger a rematch.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import AnswerBank, ProfileSuggestion
from apps.accounts.regions import REGION_LABELS
from apps.accounts.services.answer_resolver import delete_answer, write_answer
from apps.accounts.services.learning import LEARNER_VERSION, fingerprint, learn_for_profile
from apps.accounts.tiering import Tier, classify_tier, higher_tier

from .forms_profile import LearningSettingsForm

LEGACY_PREFIX = "legacy:"
_LOCKED_TIERS = (Tier.T0_LEGAL, Tier.T1_COMMERCIAL)


def _learned_rows(profile):
    return (
        AnswerBank.objects.filter(profile=profile, source=AnswerBank.Source.LEARNED)
        .exclude(question_key__startswith=LEGACY_PREFIX)
        .order_by("question_text")
    )


def _display(value):
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)


@login_required
def learning(request):
    profile = request.user.profile
    rows = list(_learned_rows(profile))
    for row in rows:
        row.value_display = _display(row.value)
        row.scope_label = REGION_LABELS.get(row.scope_region, "")
        row.evidence_count = (row.source_detail or {}).get("count")
        row.evidence_employers = [e for e in (row.source_detail or {}).get("employers", []) if e]
        row.will_lock = higher_tier(row.risk_tier, classify_tier(row.question_text)) in _LOCKED_TIERS
    suggestions = list(
        ProfileSuggestion.objects.filter(
            profile=profile, status=ProfileSuggestion.Status.PENDING
        ).order_by("question_text")
    )
    for suggestion in suggestions:
        suggestion.value_display = _display(suggestion.value)
        suggestion.scope_label = REGION_LABELS.get(suggestion.scope_region, "")
        suggestion.evidence_employers = [
            e for e in (suggestion.evidence or {}).get("employers", []) if e
        ]
    return render(
        request,
        "web/profile_learning.html",
        {
            "active_tab": "learning",
            "form": LearningSettingsForm(instance=profile),
            "learning_enabled": profile.learning_enabled,
            "learned_rows": rows,
            "suggestions": suggestions,
        },
    )


def _back():
    return redirect("profile_learning")


@login_required
@require_POST
def learning_settings(request):
    profile = request.user.profile
    form = LearningSettingsForm(request.POST, instance=profile)
    if form.is_valid():
        form.save()
        messages.success(
            request,
            "Learning is on." if profile.learning_enabled else
            "Learning is off. Nothing new is learned and learned answers are not used.",
        )
    return _back()


@login_required
@require_POST
def learning_run(request):
    profile = request.user.profile
    if not profile.learning_enabled:
        messages.error(request, "Learning is off. Turn it on to learn from your applications.")
        return _back()
    report = learn_for_profile(profile)
    if report.changed:
        messages.success(
            request,
            f"Learned {len(report.written)}, withdrew {len(report.withdrawn)} and "
            f"suggested {len(report.suggested)} from your applications.",
        )
    else:
        messages.info(
            request,
            "Nothing new to learn yet. An answer is learned once you give the same one "
            "to different employers.",
        )
    return _back()


def _reject(profile, row):
    """Record "do not propose this value again" for a learned row."""
    detail = row.source_detail or {}
    suggestion, _ = ProfileSuggestion.objects.get_or_create(
        profile=profile,
        question_key=row.question_key,
        scope_region=row.scope_region,
        value_fingerprint=fingerprint(row.value),
        defaults={
            "question_text": row.question_text,
            "value": row.value,
            "tier": row.risk_tier,
            "evidence": {k: detail[k] for k in ("employers", "job_ids", "count") if k in detail},
            "confidence": row.confidence,
            "learner_version": detail.get("learner_version", LEARNER_VERSION),
        },
    )
    suggestion.status = ProfileSuggestion.Status.REJECTED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=["status", "resolved_at", "updated_at"])


@login_required
@require_POST
def learned_promote(request, pk):
    """Make a learned answer the user's own ("use from now on")."""
    profile = request.user.profile
    row = get_object_or_404(_learned_rows(profile), pk=pk)
    lock = higher_tier(row.risk_tier, classify_tier(row.question_text)) in _LOCKED_TIERS
    write_answer(
        profile,
        row.question_text,
        row.value,
        AnswerBank.Source.USER,
        question_key=row.question_key,
        scope_region=row.scope_region,
        is_locked=lock,
        applies_everywhere=bool((row.source_detail or {}).get("applies_everywhere")),
        source_detail={"origin": "learning_accept", "learned_from": row.source_detail},
    )
    ProfileSuggestion.objects.filter(
        profile=profile,
        question_key=row.question_key,
        scope_region=row.scope_region,
        value_fingerprint=fingerprint(row.value),
        status=ProfileSuggestion.Status.PENDING,
    ).update(status=ProfileSuggestion.Status.ACCEPTED, resolved_at=timezone.now())
    messages.success(request, "Saved as your answer." + (" It is locked." if lock else ""))
    return _back()


@login_required
@require_POST
def learned_forget(request, pk):
    profile = request.user.profile
    row = get_object_or_404(_learned_rows(profile), pk=pk)
    _reject(profile, row)
    delete_answer(row, deleted_by="user_forget")
    messages.success(request, "Forgotten. It will not be suggested again unless your answers change.")
    return _back()


@login_required
@require_POST
def learned_forget_all(request):
    profile = request.user.profile
    count = 0
    for row in list(_learned_rows(profile)):
        _reject(profile, row)
        delete_answer(row, deleted_by="user_forget")
        count += 1
    messages.success(request, f"Forgot {count} learned answer{'s' if count != 1 else ''}.")
    return _back()


def _pending_suggestion(request, pk):
    return get_object_or_404(
        ProfileSuggestion,
        pk=pk,
        profile=request.user.profile,
        status=ProfileSuggestion.Status.PENDING,
    )


@login_required
@require_POST
def suggestion_accept(request, pk):
    """Promote a suggested legal/commercial answer to a locked answer of the user's."""
    suggestion = _pending_suggestion(request, pk)
    lock = higher_tier(suggestion.tier, classify_tier(suggestion.question_text)) in _LOCKED_TIERS
    write_answer(
        suggestion.profile,
        suggestion.question_text,
        suggestion.value,
        AnswerBank.Source.USER,
        question_key=suggestion.question_key,
        scope_region=suggestion.scope_region,
        is_locked=lock,
        min_tier=suggestion.tier,
        source_detail={"origin": "learning_accept", "evidence": suggestion.evidence},
    )
    suggestion.status = ProfileSuggestion.Status.ACCEPTED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=["status", "resolved_at", "updated_at"])
    messages.success(request, "Saved as your answer." + (" It is locked." if lock else ""))
    return _back()


@login_required
@require_POST
def suggestion_dismiss(request, pk):
    suggestion = _pending_suggestion(request, pk)
    suggestion.status = ProfileSuggestion.Status.REJECTED
    suggestion.resolved_at = timezone.now()
    suggestion.save(update_fields=["status", "resolved_at", "updated_at"])
    messages.info(request, "Dismissed. It will not be suggested again.")
    return _back()
