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
