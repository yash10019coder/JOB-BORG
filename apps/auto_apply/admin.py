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


@admin.register(AutoApplyDraft)
class AutoApplyDraftAdmin(admin.ModelAdmin):
    list_display = ("user", "job", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("user__username", "job__title")
