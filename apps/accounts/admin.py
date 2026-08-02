from django.contrib import admin

from .models import Profile


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
        to fall back on. Routes through `Profile.set_resume()` when the
        upload changed so this is the one call site that actually enqueues
        parsing, mirroring the future profile-edit view in apps/web.
        """
        resume_changed = "resume" in form.changed_data
        if resume_changed and obj.resume:
            obj.set_resume(obj.resume)
            return

        if resume_changed and not obj.resume:
            obj.resume_text = ""

        super().save_model(request, obj, form, change)
