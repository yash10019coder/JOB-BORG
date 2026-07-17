from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "remote_pref", "is_active", "updated_at")
    list_filter = ("remote_pref", "is_active")
    search_fields = ("user__username", "full_name")
