"""The two profile pages: search criteria and auto-apply answers."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import AnswerBank, Profile
from apps.accounts.regions import REGION_LABELS, country_choices
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import delete_answer, write_answer

from .forms_profile import AnswersSettingsForm, CustomAnswerForm, ProfileSearchForm

LEGACY_PREFIX = "legacy:"


@login_required
def profile(request):
    """Search criteria and the resume. A save rematches and shows recommendations."""
    instance = request.user.profile  # always the requesting user's own profile
    if request.method == "POST":
        form = ProfileSearchForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()  # a full save -> Profile post-save signal -> debounced rematch
            return redirect("recommendations")
    else:
        form = ProfileSearchForm(instance=instance)
    return render(request, "web/profile_form.html", {"form": form, "active_tab": "profile"})


def _answer_rows(profile_obj):
    rows = list(AnswerBank.objects.filter(profile=profile_obj).order_by("question_text"))
    for row in rows:
        row.value_display = (
            ", ".join(map(str, row.value)) if isinstance(row.value, (list, tuple)) else str(row.value)
        )
        row.scope_label = REGION_LABELS.get(row.scope_region, "")
        row.is_legacy = row.question_key.startswith(LEGACY_PREFIX)
    return rows


def _answers_context(request, form, custom_form=None):
    profile_obj = request.user.profile
    rows = _answer_rows(profile_obj)
    legacy_rows = [row for row in rows if row.is_legacy]
    custom_rows = [row for row in rows if not row.is_legacy]
    groups = [
        (value, label, [row for row in custom_rows if row.category == value])
        for value, label in AnswerBank.Category.choices
    ]
    return {
        "form": form,
        "custom_form": custom_form or CustomAnswerForm(),
        "groups": groups,
        "custom_count": len(custom_rows),
        "legacy_rows": legacy_rows,
        "conflicts": typed_facts.legacy_conflicts(profile_obj, legacy_rows),
        "visa_rows": form.visa_rows(),
        "country_choices": country_choices(),
        "visa_status_choices": Profile.VisaStatus.choices,
        "active_tab": "answers",
    }


@login_required
def profile_answers(request):
    """What auto-apply fills into forms. Saving never rematches (matching reads
    none of these fields)."""
    instance = request.user.profile
    if request.method == "POST":
        form = AnswersSettingsForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, "Your answers were saved.")
            return redirect("profile_answers")
        # Invalid: re-render the *bound* form so the errors and the user's
        # input are kept.
    else:
        form = AnswersSettingsForm(instance=instance)
    return render(request, "web/profile_answers.html", _answers_context(request, form))


def _answers_redirect(anchor="custom"):
    return redirect(f"{reverse('profile_answers')}#{anchor}")


@login_required
@require_POST
def custom_answer_create(request):
    profile_obj = request.user.profile
    custom_form = CustomAnswerForm(request.POST)
    if not custom_form.is_valid():
        return render(
            request,
            "web/profile_answers.html",
            _answers_context(request, AnswersSettingsForm(instance=profile_obj), custom_form),
        )
    cleaned = custom_form.cleaned_data
    write_answer(
        profile_obj,
        cleaned["question_text"],
        cleaned["value"],
        AnswerBank.Source.USER,
        source_detail={"origin": "profile_page"},
        **custom_form.write_kwargs(),
    )
    messages.success(request, "Answer saved.")
    return _answers_redirect()


def _own_row(request, pk):
    return get_object_or_404(AnswerBank, pk=pk, profile=request.user.profile)


@login_required
@require_POST
def custom_answer_update(request, pk):
    row = _own_row(request, pk)
    if row.question_key.startswith(LEGACY_PREFIX):
        messages.error(request, "Saved answers from the old page can only be deleted.")
        return _answers_redirect("legacy")
    data = request.POST.copy()
    data["question_text"] = row.question_text  # the question itself is the row's identity
    custom_form = CustomAnswerForm(data)
    if not custom_form.is_valid():
        messages.error(request, "That answer could not be saved: " + "; ".join(
            error for errors in custom_form.errors.values() for error in errors
        ))
        return _answers_redirect()
    cleaned = custom_form.cleaned_data
    kwargs = custom_form.write_kwargs()
    # The region and "applies everywhere" flag belong to the row, not the edit.
    kwargs["scope_region"] = row.scope_region
    kwargs["applies_everywhere"] = bool((row.source_detail or {}).get("applies_everywhere"))
    write_answer(
        request.user.profile,
        row.question_text,
        cleaned["value"],
        AnswerBank.Source.USER,
        question_key=row.question_key,
        source_detail={**(row.source_detail or {}), "origin": "profile_page"},
        **kwargs,
    )
    messages.success(request, "Answer updated.")
    return _answers_redirect()


@login_required
@require_POST
def custom_answer_delete(request, pk):
    row = _own_row(request, pk)
    anchor = "legacy" if row.question_key.startswith(LEGACY_PREFIX) else "custom"
    delete_answer(row)
    messages.success(request, "Answer deleted.")
    return _answers_redirect(anchor)
