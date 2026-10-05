"""Web URL routes."""
from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from . import views, views_import, views_learning, views_profile

urlpatterns = [
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", views.signup, name="signup"),
    path("profile/", views_profile.profile, name="profile"),
    path("profile/answers/", views_profile.profile_answers, name="profile_answers"),
    path("profile/import/", views_import.profile_import, name="profile_import"),
    path("profile/import/start/", views_import.import_start, name="import_start"),
    path("profile/import/entries/<int:pk>/delete/", views_import.import_entry_delete, name="import_entry_delete"),
    path("profile/import/<uuid:public_id>/", views_import.import_status, name="import_status"),
    path("profile/import/<uuid:public_id>/review/", views_import.import_review, name="import_review"),
    path("profile/import/<uuid:public_id>/apply/", views_import.import_apply, name="import_apply"),
    path("profile/import/<uuid:public_id>/discard/", views_import.import_discard, name="import_discard"),
    path("profile/learning/", views_learning.learning, name="profile_learning"),
    path("profile/learning/settings/", views_learning.learning_settings, name="learning_settings"),
    path("profile/learning/run/", views_learning.learning_run, name="learning_run"),
    path("profile/learning/forget-all/", views_learning.learned_forget_all, name="learned_forget_all"),
    path("profile/learning/answers/<int:pk>/promote/", views_learning.learned_promote, name="learned_promote"),
    path("profile/learning/answers/<int:pk>/forget/", views_learning.learned_forget, name="learned_forget"),
    path("profile/learning/suggestions/<int:pk>/accept/", views_learning.suggestion_accept, name="suggestion_accept"),
    path("profile/learning/suggestions/<int:pk>/dismiss/", views_learning.suggestion_dismiss, name="suggestion_dismiss"),
    path("profile/answers/quick-fill/", views_profile.quick_fill, name="quick_fill"),
    path(
        "profile/answers/custom/",
        views_profile.custom_answer_create,
        name="custom_answer_create",
    ),
    path(
        "profile/answers/custom/<int:pk>/",
        views_profile.custom_answer_update,
        name="custom_answer_update",
    ),
    path(
        "profile/answers/custom/<int:pk>/delete/",
        views_profile.custom_answer_delete,
        name="custom_answer_delete",
    ),
    # The old "saved answers" page is gone; bookmarks land on the new page.
    path(
        "profile/explicit-answers/",
        RedirectView.as_view(pattern_name="profile_answers", permanent=False),
        name="explicit_answers",
    ),
    # Recommendations list + save/dismiss/mark-applied actions (U12).
    path("", views.recommendations, name="recommendations"),
    path("jobs/<int:job_id>/action/", views.job_action, name="job_action"),
    # Auto-apply trigger, review queue, edit, and send (U8).
    path("auto-apply/jobs/<int:job_id>/trigger/", views.trigger_auto_apply, name="trigger_auto_apply"),
    path("auto-apply/queue/", views.auto_apply_queue, name="auto_apply_queue"),
    path("auto-apply/drafts/<int:pk>/answers/", views.edit_auto_apply_draft, name="edit_auto_apply_draft"),
    path("auto-apply/drafts/<int:pk>/discard/", views.discard_auto_apply_draft, name="discard_auto_apply_draft"),
    path("auto-apply/drafts/<int:pk>/send/", views.send_auto_apply_draft, name="send_auto_apply_draft"),
    path(
        "auto-apply/drafts/<int:pk>/confirm-learned/",
        views.confirm_learned_answers,
        name="confirm_learned_answers",
    ),
    path("settings/email-credential/", views.email_inbox_credential, name="email_inbox_credential"),
    path("settings/email-credential/delete/", views.delete_email_inbox_credential, name="delete_email_inbox_credential"),
]


