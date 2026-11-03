from django.contrib import admin

from .models import AutoApplyDraft, ExplicitAnswer


@admin.register(ExplicitAnswer)
class ExplicitAnswerAdmin(admin.ModelAdmin):
    # answer_text is deliberately excluded from list_display/search_fields --
    # sensitive-category answers (e.g. work authorization) shouldn't be
    # casually browsable in a list view. Full detail-view access remains.
    list_display = ("user", "category", "updated_at")
    list_filter = ("category",)
    search_fields = ("user__username",)

    # Retired: the page that wrote these is gone and the resolver reads the
    # backfilled `legacy:*` AnswerBank rows. The table is kept, unwritten,
    # until it is dropped (Phase 5), so nothing here may change it.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AutoApplyDraft)
class AutoApplyDraftAdmin(admin.ModelAdmin):
    list_display = ("user", "job", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("user__username", "job__title")
