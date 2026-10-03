from django.contrib import admin

from .models import AnswerBank, AnswerBankHistory, EmailInboxCredential, Profile


class UnresolvedTargetLocationFilter(admin.SimpleListFilter):
    """Profiles with at least one target_locations_normalized entry the
    location alias table couldn't resolve -- same curation-visibility signal
    as JobAdmin's location_resolved filter."""

    title = "has unresolved target location"
    parameter_name = "unresolved_target_location"

    def lookups(self, request, model_admin):
        return (("yes", "Yes"),)

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(target_locations_normalized__contains=[{"resolved": False}])
        return queryset


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "remote_pref", "is_active", "updated_at")
    list_filter = ("remote_pref", "is_active", UnresolvedTargetLocationFilter)
    search_fields = ("user__username", "full_name")
    readonly_fields = ("resume_text",)

    def save_model(self, request, obj, form, change):
        """The admin is a write path to `Profile.resume` too (see U1), so it
        must trigger the same explicit parse -- there's no post_save signal
        to fall back on. Routes through `Profile.set_resume()` for *every*
        resume change (add or clear) so this stays the one call site that
        actually mutates `resume`/`resume_text`, rather than a second,
        divergent clear-path that bypasses `full_clean()`/the explicit-
        trigger convention `set_resume()` exists to centralize.
        """
        if "resume" in form.changed_data:
            # Save every other field first (this also creates the row on the
            # add view), then apply the resume change through set_resume() --
            # which only saves resume fields -- so nothing else edited in the
            # same submit is silently dropped. Mirrors ProfileForm.save().
            new_resume = form.cleaned_data.get("resume")
            obj.resume = form.initial.get("resume") or None
            super().save_model(request, obj, form, change)
            obj.set_resume(new_resume)
            return

        super().save_model(request, obj, form, change)


@admin.register(EmailInboxCredential)
class EmailInboxCredentialAdmin(admin.ModelAdmin):
    list_display = ("user", "email_address", "is_active", "last_error_code", "updated_at")
    search_fields = ("user__username", "email_address")
    # The ciphertext must never be renderable or editable in admin, not even
    # as ciphertext -- a visible field invites a future "just show it
    # decrypted" mistake. `exclude`, not `readonly_fields`, since even the
    # ciphertext shouldn't render at all (contrast ProfileAdmin's
    # `readonly_fields = ("resume_text",)`, which is fine to display).
    exclude = ("app_password_encrypted",)


@admin.register(AnswerBank)
class AnswerBankAdmin(admin.ModelAdmin):
    list_display = ("profile", "question_key", "source", "risk_tier", "is_locked", "updated_at")
    list_filter = ("source", "risk_tier", "is_locked")
    search_fields = ("question_key", "profile__user__username")


@admin.register(AnswerBankHistory)
class AnswerBankHistoryAdmin(admin.ModelAdmin):
    """Audit trail: browsable, never editable from the admin."""

    list_display = ("profile", "question_key", "source", "superseded_by_source", "superseded_at")
    search_fields = ("question_key", "profile__user__username")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
