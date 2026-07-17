"""Web URL routes."""
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", views.signup, name="signup"),
    path("profile/", views.profile, name="profile"),
    # Recommendations list + save/dismiss/mark-applied actions (U12).
    path("", views.recommendations, name="recommendations"),
    path("jobs/<int:job_id>/action/", views.job_action, name="job_action"),
]
