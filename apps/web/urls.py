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
    # Auto-apply trigger, review queue, edit, and send (U8).
    path("auto-apply/jobs/<int:job_id>/trigger/", views.trigger_auto_apply, name="trigger_auto_apply"),
    path("auto-apply/queue/", views.auto_apply_queue, name="auto_apply_queue"),
    path("auto-apply/drafts/<int:pk>/answers/", views.edit_auto_apply_draft, name="edit_auto_apply_draft"),
    path("auto-apply/drafts/<int:pk>/send/", views.send_auto_apply_draft, name="send_auto_apply_draft"),
]
