"""The Import tab: start an import, wait for it, review the proposals, apply them.

Every job is looked up by its public id *through the requesting user's profile*
(404 otherwise), every change is a login-protected POST (so CSRF applies), and
nothing here writes to the profile except ``review.apply_review``, which only
writes what the user accepted. A ``next`` parameter is never honoured.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.importing import review as review_logic
from apps.accounts.importing import github, llm_gate, service
from apps.accounts.importing.documents import DocumentError
from apps.accounts.models import ImportJob, ResumeEntry
from apps.accounts.services.resume_facts import delete_entry, format_years, years_by_skill

REFRESH_SECONDS = 3
REFRESH_WINDOW_SECONDS = 180  # stop auto-refreshing after three minutes

SOURCE_CHOICES = {
    "resume": ImportJob.SourceKind.RESUME,
    "linkedin_pdf": ImportJob.SourceKind.LINKEDIN_PDF,
    "current_resume": ImportJob.SourceKind.CURRENT_RESUME,
}


def _own_job(request, public_id):
    return get_object_or_404(ImportJob, public_id=public_id, profile=request.user.profile)


def _consent_current(profile):
    return bool(
        profile.llm_import_consent_at
        and profile.llm_import_consent_version == settings.PROFILE_IMPORT_CONSENT_VERSION
    )


@login_required
def profile_import(request):
    profile = request.user.profile
    jobs = list(ImportJob.objects.filter(profile=profile)[:5])
    entries = list(ResumeEntry.objects.filter(profile=profile))
    years = [
        {"name": item.name, "text": format_years(item.months), "approximate": item.approximate}
        for item in years_by_skill(profile)
    ]
    return render(
        request,
        "web/profile_import.html",
        {
            "active_tab": "import",
            "jobs": jobs,
            "experience": [e for e in entries if e.kind == ResumeEntry.Kind.EXPERIENCE],
            "projects": [e for e in entries if e.kind == ResumeEntry.Kind.PROJECT],
            "years": years,
            "has_saved_resume": bool((profile.resume_text or "").strip()),
            # AI-assisted import is offered only when an operator has enabled a provider.
            "llm_available": llm_gate.provider_available(),
            "llm_provider": settings.PROFILE_IMPORT_LLM_PROVIDER,
            "llm_consented": _consent_current(profile),
            "llm_consent_outdated": bool(profile.llm_import_consent_at) and not _consent_current(profile),
        },
    )


@login_required
@require_POST
def import_start(request):
    source = SOURCE_CHOICES.get(request.POST.get("source", ""))
    if source is None:
        messages.error(request, "Choose what to import.")
        return redirect("profile_import")
    try:
        job = service.start_document_import(
            request.user.profile, source, upload=request.FILES.get("file")
        )
    except DocumentError as exc:
        messages.error(request, service.error_message(exc.code))
        return redirect("profile_import")
    except service.ImportInProgress:
        messages.error(request, "An import is already running. Wait for it to finish.")
        return redirect("profile_import")
    except service.ImportRateLimited:
        messages.error(request, "You have started a lot of imports. Try again in an hour.")
        return redirect("profile_import")
    return redirect("import_status", public_id=job.public_id)


@login_required
def import_status(request, public_id):
    job = _own_job(request, public_id)
    if job.status == ImportJob.Status.READY:
        return redirect("import_review", public_id=job.public_id)
    age = (timezone.now() - job.created_at).total_seconds()
    waiting = job.status in ImportJob.IN_FLIGHT
    return render(
        request,
        "web/profile_import_status.html",
        {
            "active_tab": "import",
            "job": job,
            "waiting": waiting,
            "refresh": REFRESH_SECONDS if waiting and age < REFRESH_WINDOW_SECONDS else 0,
            "error_text": service.error_message(job.error_code) if job.error_code else "",
        },
    )


def _review_context(request, job, review, errors=None):
    return {
        "active_tab": "import",
        "job": job,
        "review": review,
        "errors": errors or {},
        "has_fields": bool(review.fields),
        "has_entries": bool(review.entries),
    }


@login_required
def import_review(request, public_id):
    job = _own_job(request, public_id)
    if job.status != ImportJob.Status.READY or job.expires_at <= timezone.now():
        return redirect("import_status", public_id=job.public_id)
    return render(
        request, "web/profile_import_review.html", _review_context(request, job, review_logic.build_review(job))
    )


def _summary(outcome):
    parts = []
    if outcome.applied:
        parts.append(f"Saved {len(outcome.applied)} profile field{'s' if len(outcome.applied) != 1 else ''}")
    stored = outcome.entries_created + outcome.entries_updated
    if stored:
        parts.append(f"saved {stored} experience/project entr{'ies' if stored != 1 else 'y'}")
    kept = len(outcome.kept) + outcome.entries_kept
    if kept:
        parts.append(f"kept {kept} you had already set")
    return (", ".join(parts).capitalize() + ".") if parts else "Nothing was changed."


@login_required
@require_POST
def import_apply(request, public_id):
    job = _own_job(request, public_id)
    try:
        outcome = review_logic.apply_review(job, request.POST)
    except review_logic.ReviewUnavailable:
        messages.error(request, "This import can no longer be applied.")
        return redirect("import_status", public_id=job.public_id)
    except review_logic.ReviewError as exc:
        job.refresh_from_db()
        review = review_logic.build_review(job, posted=request.POST)
        for row in review.fields:
            row.error = exc.field_errors.get(row.key, "")
        for row in review.entries:
            row.error = exc.entry_errors.get(row.index, "")
        messages.error(request, "Some values need fixing. Nothing was saved.")
        return render(
            request,
            "web/profile_import_review.html",
            _review_context(request, job, review, errors={**exc.field_errors, **exc.entry_errors}),
        )
    messages.success(request, _summary(outcome))
    return redirect("profile_import")


@login_required
@require_POST
def import_discard(request, public_id):
    job = _own_job(request, public_id)
    try:
        review_logic.discard(job)
    except review_logic.ReviewUnavailable:
        messages.error(request, "This import is already closed.")
    else:
        messages.info(request, "Import discarded. Nothing was changed.")
    return redirect("profile_import")


@login_required
@require_POST
def import_entry_delete(request, pk):
    if not delete_entry(request.user.profile, pk):
        raise Http404("No such entry.")
    messages.info(request, "Entry deleted.")
    return redirect("profile_import")


@login_required
@require_POST
def import_consent(request):
    """Opt in to (or out of) AI-assisted import. Off by default; only offered when
    an operator has enabled a provider. Saved with ``update_fields`` so it never
    triggers a rematch."""
    profile = request.user.profile
    if not llm_gate.provider_available():
        messages.error(request, "AI-assisted import is not available.")
        return redirect("profile_import")
    if request.POST.get("consent"):
        profile.llm_import_consent_at = timezone.now()
        profile.llm_import_consent_version = settings.PROFILE_IMPORT_CONSENT_VERSION
        messages.success(request, "AI-assisted import is on for your future imports.")
    else:
        profile.llm_import_consent_at = None
        profile.llm_import_consent_version = ""
        messages.info(request, "AI-assisted import is off. Imports use rules only.")
    profile.save(update_fields=["llm_import_consent_at", "llm_import_consent_version"])
    return redirect("profile_import")


@login_required
@require_POST
def import_github(request):
    """Start an import from a public GitHub username (no sign-in with GitHub)."""
    try:
        job = service.start_github_import(request.user.profile, request.POST.get("username", ""))
    except github.GitHubError as exc:
        messages.error(request, service.error_message(exc.code))
        return redirect("profile_import")
    except service.ImportInProgress:
        messages.error(request, "An import is already running. Wait for it to finish.")
        return redirect("profile_import")
    except service.ImportRateLimited:
        messages.error(request, "You have started a lot of imports. Try again in an hour.")
        return redirect("profile_import")
    return redirect("import_status", public_id=job.public_id)
