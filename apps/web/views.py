"""Web views: signup, profile setup (U11), recommendations + actions (U12)."""
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render

from .forms import ProfileForm


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


# --- Recommendations (fleshed out in U12) ---------------------------------


@login_required
def recommendations(request):  # replaced by the full implementation in U12
    return render(request, "web/recommendations.html", {"page_obj": None})


@login_required
def job_action(request, job_id):  # implemented in U12
    return redirect("recommendations")
