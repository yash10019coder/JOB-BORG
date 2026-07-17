from django.contrib import admin

from .models import JobApplication


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ("user", "job", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("user__username", "job__title")
