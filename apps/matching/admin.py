from django.contrib import admin

from .models import UserJobMatch


@admin.register(UserJobMatch)
class UserJobMatchAdmin(admin.ModelAdmin):
    list_display = ("user", "job", "match_score", "match_status", "computed_at")
    list_filter = ("match_status",)
    search_fields = ("user__username", "job__title")
    autocomplete_fields = ()
