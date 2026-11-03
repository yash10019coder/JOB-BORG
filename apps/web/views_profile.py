"""The two profile pages: search criteria and auto-apply answers."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts import question_semantics
from apps.accounts.models import AnswerBank, Profile
from apps.accounts.regions import REGION_LABELS, country_choices
from apps.accounts.services import typed_facts
from apps.accounts.services.answer_resolver import (
    delete_answer,
    normalize_question_key,
    write_answer,
)
from apps.accounts.tiering import Tier, classify_tier
from apps.auto_apply.greenhouse_form.field_mapping import (
    CHECKBOX_GROUP,
    COMBOBOX_SELECT,
    FILE,
    MULTI_SELECT,
    SINGLE_SELECT,
    TEXTAREA,
)
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.services import questions_panel
from apps.auto_apply.services.confirmation import is_blank_answer_value

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
    # Learned rows live on the Learning tab, where they can be promoted or forgotten.
    rows = list(
        AnswerBank.objects.filter(profile=profile_obj)
        .exclude(source=AnswerBank.Source.LEARNED)
        .order_by("question_text")
    )
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
        "learned_count": AnswerBank.objects.filter(
            profile=profile_obj, source=AnswerBank.Source.LEARNED
        ).exclude(question_key__startswith=LEGACY_PREFIX).count(),
        "legacy_rows": legacy_rows,
        "conflicts": typed_facts.legacy_conflicts(profile_obj, legacy_rows),
        "visa_rows": form.visa_rows(),
        "citizenship_rows": form.citizenship_rows(),
        "country_choices": country_choices(),
        "visa_status_choices": Profile.VisaStatus.choices,
        "panel": questions_panel.get_panel(request.user),
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


# --- Quick-fill: answer a question the panel shows as unanswered ------------

_OPTION_FIELD_TYPES = (SINGLE_SELECT, COMBOBOX_SELECT, MULTI_SELECT, CHECKBOX_GROUP)


def _quick_fill_target(request, draft_id, field_index):
    """``(draft, field dict)`` from the request's ids, or 404. The question,
    its options and the prefill are all re-derived from the user's own draft:
    nothing about them is trusted from the request."""
    try:
        draft = get_object_or_404(
            AutoApplyDraft.objects.select_related("job", "job__employer"),
            pk=int(draft_id),
            user=request.user,
        )
        fields = (draft.form_schema_snapshot or {}).get("fields") or []
        position = int(field_index)
        if position < 0:  # a negative index would silently pick the last field
            raise IndexError(position)
        field = fields[position]
    except (TypeError, ValueError, IndexError):
        raise Http404("No such question.")
    return draft, field


def _quick_fill_context(request, draft, field, *, errors=None, posted=None):
    label = field.get("label") or ""
    options = tuple(field.get("options") or ())
    entry = (draft.answers or {}).get(label) or {}
    provenance = entry.get("provenance") or {}
    prefill = entry.get("value")
    from_user = bool(entry.get("user_confirmed")) or provenance.get("source") == "user"
    has_prefill = not is_blank_answer_value(prefill)
    tier = classify_tier(label)
    employer = None if field.get("field_type") == TEXTAREA else draft.job.employer.name
    sensitive = question_semantics.is_location_sensitive(label)
    region = typed_facts.job_region(draft.job)
    existing = None
    if not sensitive or region:
        existing = AnswerBank.objects.filter(
            profile=request.user.profile,
            question_key=normalize_question_key(label, employer),
            scope_region=region if sensitive else "",
        ).first()
    return {
        "draft": draft,
        "field_index": request.GET.get("field") or request.POST.get("field"),
        "label": label,
        "field_type": field.get("field_type"),
        "options": options,
        "prefill": (posted or {}).get("value", prefill if has_prefill else ""),
        "prefill_source": provenance.get("source") or ("a draft answer" if has_prefill else ""),
        "needs_confirm_box": has_prefill and not from_user and tier in (Tier.T0_LEGAL, Tier.T1_COMMERCIAL),
        "tier": tier,
        "sensitive": sensitive,
        "region_label": REGION_LABELS.get(region, ""),
        "existing": existing,
        "errors": errors or [],
        "active_tab": "answers",
    }


def _clean_quick_value(field, post):
    """``(value, error)``: the answer, validated against the form's options."""
    field_type = field.get("field_type")
    options = tuple(field.get("options") or ())
    if field_type in (MULTI_SELECT, CHECKBOX_GROUP):
        value = [v for v in post.getlist("value") if v.strip()]
        if not value:
            return None, "Choose at least one answer."
        if options and any(v not in options for v in value):
            return None, "Choose answers from this form's options."
        return value, None
    value = (post.get("value") or "").strip()
    if not value:
        return None, "An answer cannot be blank."
    enforced = field_type in (SINGLE_SELECT,) or (
        field_type == COMBOBOX_SELECT and field.get("options_complete")
    )
    if options and enforced and value not in options:
        return None, "Choose one of this form's options."
    return value[:2000], None


@login_required
def quick_fill(request):
    """Save an unanswered panel question as a reusable answer.

    Works without JavaScript: a plain page with the question, the answer
    drafting proposed (prefilled, editable) and a confirm step. Settings-backed
    questions are refused here (change the setting instead), long free text
    is employer-specific, and a legal/commercial value that did not come from
    the user needs its own explicit confirmation before it is saved and locked.
    """
    source = request.POST if request.method == "POST" else request.GET
    draft, field = _quick_fill_target(request, source.get("draft"), source.get("field"))
    label = field.get("label") or ""
    profile_obj = request.user.profile

    if field.get("field_type") in (FILE, TEXTAREA):
        messages.error(request, "That kind of answer can't be saved for reuse.")
        return _answers_redirect("panel")
    options = tuple(field.get("options") or ())
    if typed_facts.resolve(profile_obj, label, options=options, job=draft.job) is not None or (
        question_semantics.blocks_llm(label)
    ):
        messages.info(request, "That question is answered from your settings — change it there.")
        return _answers_redirect("work-auth")

    context = _quick_fill_context(request, draft, field)
    if request.method != "POST":
        return render(request, "web/profile_quick_fill.html", context)

    errors = []
    value, error = _clean_quick_value(field, request.POST)
    if error:
        errors.append(error)
    if context["needs_confirm_box"] and "confirm_value" not in request.POST:
        errors.append(
            "This answer was not entered by you and is legal or commercial. "
            "Tick the box to confirm it is correct before it is saved."
        )
    if context["existing"] is not None and "overwrite" not in request.POST:
        errors.append("You already have an answer to this question. Tick the box to replace it.")
    scope, everywhere = "", False
    if context["sensitive"]:
        if "everywhere" in request.POST:
            everywhere = True
        elif typed_facts.job_region(draft.job):
            scope = typed_facts.job_region(draft.job)
        else:
            errors.append("The job's region isn't known: tick 'any region' to save this answer.")
    if errors:
        context["errors"] = errors
        context["prefill"] = value if value is not None else request.POST.get("value", "")
        return render(request, "web/profile_quick_fill.html", context)

    entry = (draft.answers or {}).get(label) or {}
    employer = None if field.get("field_type") == TEXTAREA else draft.job.employer.name
    write_answer(
        profile_obj,
        label,
        value,
        AnswerBank.Source.USER,
        options=options,
        source_detail={
            "origin": "quick_fill",
            "draft_id": draft.pk,
            "prefill_provenance": entry.get("provenance"),
        },
        is_locked=True,
        scope_region=scope,
        applies_everywhere=everywhere,
        employer=employer,
    )
    messages.success(request, "Saved. Auto-apply will use this answer for the same question.")
    return _answers_redirect("panel")
