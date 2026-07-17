"""Web views: signup, profile setup (U11), recommendations + actions (U12)."""
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.applications.models import JobApplication
from apps.jobs.models import Job
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
    user (including below-threshold), each card labelled by status. Dismissed
    jobs are hidden either way; each card carries the user's action state.
    """
    user = request.user
    show_all = request.GET.get("all") == "1"

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
        {"page_obj": page_obj, "show_all": show_all},
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
    return redirect("recommendations")
