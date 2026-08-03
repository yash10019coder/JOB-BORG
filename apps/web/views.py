"""Web views: signup, profile setup (U11), recommendations + actions (U12),
auto-apply trigger/queue/edit/send (U8)."""
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.applications.models import JobApplication
from apps.auto_apply.greenhouse_form.field_mapping import FILE
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.tasks import draft_auto_apply, submit_auto_apply_draft
from apps.jobs.models import JOB_SEARCH_CONFIG, Job, JobSource
from apps.matching.constants import MatchStatus
from apps.matching.models import UserJobMatch

from .forms import ProfileForm

RECOMMENDATIONS_PER_PAGE = 20

# Map an action name from the UI to a JobApplication status.
_ACTION_STATUS = {
    "save": JobApplication.Status.SAVED,
    "apply": JobApplication.Status.APPLIED,
    "dismiss": JobApplication.Status.DISMISSED,
}


def _clean_query(raw):
    """Strip NUL bytes and whitespace from a search query.

    Postgres text columns reject NUL bytes outright (DataError, not a
    validation error) — strip them so a stray %00 in the query string or
    posted form field can't 500 the page.
    """
    return raw.replace("\x00", "").strip()


def signup(request):
    if request.user.is_authenticated:
        return redirect("recommendations")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()  # accounts signal auto-creates the Profile
            login(request, user)
            return redirect("profile")
    else:
        form = UserCreationForm()
    return render(request, "registration/signup.html", {"form": form})


@login_required
def profile(request):
    instance = request.user.profile  # always the requesting user's own profile
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()  # Profile post-save signal -> debounced rematch
            return redirect("recommendations")
    else:
        form = ProfileForm(instance=instance)
    return render(request, "web/profile_form.html", {"form": form})


# --- Recommendations ------------------------------------------------------


@login_required
def recommendations(request):
    """Ranked matches for the current user.

    Defaults to recommended-only; ``?all=1`` shows every scored match for the
    user (including below-threshold), each card labelled by status. ``?q=``
    further narrows whichever set is active (title/description full-text
    search) — search layers on top of the toggle, it never bypasses it.
    Dismissed jobs are hidden either way; each card carries the user's
    action state.
    """
    user = request.user
    show_all = request.GET.get("all") == "1"
    query = _clean_query(request.GET.get("q", ""))

    dismissed_job_ids = JobApplication.objects.filter(
        user=user, status=JobApplication.Status.DISMISSED
    ).values_list("job_id", flat=True)

    matches = (
        UserJobMatch.objects.filter(user=user)
        .exclude(job_id__in=dismissed_job_ids)
        .select_related("job", "job__employer")
        .order_by("-match_score")
    )
    if not show_all:
        matches = matches.filter(match_status=MatchStatus.RECOMMENDED)
    if query:
        # config must match the GinIndex on Job (job_search_gin) exactly, or
        # this silently falls back to a sequential scan instead of using it.
        # .alias() (not .annotate()) so the tsvector expression is usable in
        # the filter without also being materialized into the SELECT list.
        matches = matches.alias(
            search=SearchVector("job__title", "job__description", config=JOB_SEARCH_CONFIG)
        ).filter(search=SearchQuery(query, config=JOB_SEARCH_CONFIG))

    paginator = Paginator(matches, RECOMMENDATIONS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Annotate each match on this page with the user's current action state.
    page_job_ids = [m.job_id for m in page_obj]
    app_status = dict(
        JobApplication.objects.filter(
            user=user, job_id__in=page_job_ids
        ).values_list("job_id", "status")
    )
    for match in page_obj:
        match.user_status = app_status.get(match.job_id, "")

    return render(
        request,
        "web/recommendations.html",
        {"page_obj": page_obj, "show_all": show_all, "query": query},
    )


@login_required
@require_POST
def job_action(request, job_id):
    """Idempotently record save/apply/dismiss for the current user's job."""
    job = get_object_or_404(Job, pk=job_id)
    status = _ACTION_STATUS.get(request.POST.get("action"))
    if status is not None:
        JobApplication.objects.update_or_create(
            user=request.user, job=job, defaults={"status": status}
        )

    # Preserve the toggle/search state the action was taken from (carried as
    # hidden fields on the action form) so Save/Apply/Dismiss doesn't silently
    # reset the user back to the unfiltered recommended-only view.
    redirect_url = reverse("recommendations")
    params = {}
    if request.POST.get("all") == "1":
        params["all"] = "1"
    query = _clean_query(request.POST.get("q", ""))
    if query:
        params["q"] = query
    if params:
        redirect_url = f"{redirect_url}?{urlencode(params)}"
    return redirect(redirect_url)


# --- Auto-apply (U8) -------------------------------------------------------

# User-facing message per `AutoApplyDraft.ReasonCode` -- keyed on the
# structured code the drafting/submission code already sets (see
# apps/auto_apply/services/drafting.py and apps/auto_apply/tasks.py), not
# derived by pattern-matching the free-text `exclusion_reason`/
# `error_message` (which exist for logs/debugging and can be reworded
# there without silently breaking this mapping).
_REASON_CODE_MESSAGES = {
    AutoApplyDraft.ReasonCode.SCHEMA_MISMATCH: (
        "This application has a question we couldn't fill in automatically."
    ),
    AutoApplyDraft.ReasonCode.FORM_LOAD_FAILED: (
        "We couldn't load this employer's application form."
    ),
    AutoApplyDraft.ReasonCode.UNANSWERABLE_REQUIRED: (
        "This application asks something we don't have an answer for yet."
    ),
    AutoApplyDraft.ReasonCode.CAPTCHA_CHALLENGED: (
        "This employer's form couldn't be completed automatically right now."
    ),
    AutoApplyDraft.ReasonCode.SUBMISSION_FAILED: (
        "The application couldn't be submitted; you can try again."
    ),
    AutoApplyDraft.ReasonCode.SENDING_TIMEOUT: (
        "Submission timed out and wasn't completed; you can try again."
    ),
    AutoApplyDraft.ReasonCode.UNEXPECTED_ERROR: (
        "Something went wrong while submitting this application."
    ),
}
_GENERIC_UNAVAILABLE_MESSAGE = "This application couldn't be completed automatically."
_STALE_MESSAGE = "This job posting closed before we could apply."


def _friendly_draft_message(draft):
    """User-facing explanation for a non-actionable draft state, keyed on
    `draft.reason_code` rather than the stored `exclusion_reason`/
    `error_message` free text (plan review flagged both raw internal text
    and prose pattern-matching as unsuitable/fragile for direct display)."""
    if draft.status == AutoApplyDraft.Status.STALE:
        return _STALE_MESSAGE
    if draft.status in (AutoApplyDraft.Status.EXCLUDED, AutoApplyDraft.Status.FAILED):
        return _REASON_CODE_MESSAGES.get(draft.reason_code, _GENERIC_UNAVAILABLE_MESSAGE)
    return ""


@login_required
@require_POST
def trigger_auto_apply(request, job_id):
    """Enqueue `draft_auto_apply` for the requesting user + job (F1).

    Mirrors `job_action`'s POST-then-redirect shape. Only reachable for
    Greenhouse-sourced jobs (`draft_for`/`draft_auto_apply` raise `ValueError`
    for anything else per U6) -- a non-Greenhouse job gets a 400 rather than
    reaching the task. Drafting is async, so the flash message says
    "drafting…" rather than promising a result; the authoritative outcome
    (including any `EXCLUDED` reason) is surfaced durably in the queue view.
    """
    job = get_object_or_404(Job, pk=job_id)
    if job.source_ats != JobSource.ATS.GREENHOUSE:
        return HttpResponseBadRequest("Auto-apply is only available for Greenhouse-sourced jobs.")

    draft_auto_apply.delay(request.user.id, job.id)
    messages.info(
        request,
        "Drafting your application… check the auto-apply queue shortly for the result.",
    )

    redirect_url = reverse("recommendations")
    params = {}
    if request.POST.get("all") == "1":
        params["all"] = "1"
    query = _clean_query(request.POST.get("q", ""))
    if query:
        params["q"] = query
    if params:
        redirect_url = f"{redirect_url}?{urlencode(params)}"
    return redirect(redirect_url)


@login_required
def auto_apply_queue(request):
    """List the requesting user's `AutoApplyDraft`s across all statuses
    (F2) -- `EXCLUDED`/`STALE` are shown, not hidden by default, so a user
    can always see why a job didn't become sendable.

    `select_related("job", "job__employer")` batch-fetches the related Job
    (and its Employer) in the same query as the drafts, following
    `recommendations`' batch-fetch pattern rather than N+1 per-draft lookups.
    """
    drafts_qs = (
        AutoApplyDraft.objects.filter(user=request.user)
        .select_related("job", "job__employer")
        .order_by("-updated_at")
    )
    paginator = Paginator(drafts_qs, RECOMMENDATIONS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    for draft in page_obj:
        draft.friendly_message = _friendly_draft_message(draft)

    return render(request, "web/auto_apply_queue.html", {"page_obj": page_obj})


@login_required
@require_POST
def edit_auto_apply_draft(request, pk):
    """Whole-draft answer edit for a `DRAFTED` draft.

    Ownership+status scoped (`user=request.user, status=DRAFTED`) so a
    request for another user's draft, or a draft that's no longer editable,
    404s rather than mutating it. The template posts one `label__<i>` /
    `value__<i>` pair per answer (indexed rather than keyed by the raw label,
    since labels are arbitrary employer-supplied text); any answer present
    in the POST has its stored value updated and `needs_review` cleared --
    the user has now confirmed it.
    """
    draft = get_object_or_404(
        AutoApplyDraft, pk=pk, user=request.user, status=AutoApplyDraft.Status.DRAFTED
    )

    answers = {label: dict(entry) for label, entry in (draft.answers or {}).items()}
    index = 0
    while f"label__{index}" in request.POST:
        label = request.POST[f"label__{index}"]
        value_field = f"value__{index}"
        if (
            label in answers
            and value_field in request.POST
            # A file-backed answer's "value" is a server-side path (or
            # storage key) that GreenhouseFormClient._fill_answers later
            # passes straight to Playwright's set_input_files() at send
            # time. Letting this endpoint overwrite it with an arbitrary
            # user-supplied string would let a user point their own
            # submission at any file readable by the Celery worker and
            # have it uploaded to a real employer -- file-type answers are
            # therefore never editable here, only re-derived from Profile
            # by re-drafting.
            and answers[label].get("field_type") != FILE
        ):
            answers[label]["value"] = request.POST[value_field]
            answers[label]["needs_review"] = False
        index += 1

    draft.answers = answers
    draft.save(update_fields=["answers", "updated_at"])
    return redirect("auto_apply_queue")


@login_required
@require_POST
def send_auto_apply_draft(request, pk):
    """Atomically transition a `DRAFTED` draft to `SENDING` and enqueue
    `submit_auto_apply_draft`.

    The guard is scoped to `user=request.user` in addition to `pk` and
    `status=DRAFTED` -- an unscoped guard would let any authenticated user
    flip another user's draft to `SENDING` by guessing/enumerating a `pk`,
    triggering submission of that user's resume/answers to an employer
    without their action. Zero rows updated (not found, wrong status, *or*
    wrong user) is a 404 in all three cases -- the response never leaks
    which case it was. Using `.filter(...).update(...)` and checking the
    row count (rather than get-then-save) is also what makes the guard
    atomic against a concurrent double-submit.
    """
    updated = AutoApplyDraft.objects.filter(
        pk=pk, user=request.user, status=AutoApplyDraft.Status.DRAFTED
    ).update(status=AutoApplyDraft.Status.SENDING)
    if not updated:
        raise Http404("Draft not found or not sendable.")

    submit_auto_apply_draft.delay(pk)
    messages.info(request, "Sending your application…")
    return redirect("auto_apply_queue")
