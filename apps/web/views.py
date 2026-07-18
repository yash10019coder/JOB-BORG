"""Web views: signup, profile setup (U11), recommendations + actions (U12)."""
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.applications.models import JobApplication
from apps.jobs.models import JOB_SEARCH_CONFIG, Job
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
    # Postgres text columns reject NUL bytes outright (DataError, not a
    # validation error) — strip them so a stray %00 in the query string can't
    # 500 the page.
    query = request.GET.get("q", "").replace("\x00", "").strip()

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
    query = request.POST.get("q", "").replace("\x00", "").strip()
    if query:
        params["q"] = query
    if params:
        redirect_url = f"{redirect_url}?{urlencode(params)}"
    return redirect(redirect_url)
