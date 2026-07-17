from django.contrib import admin

from .models import Job, JobSource


@admin.register(JobSource)
class JobSourceAdmin(admin.ModelAdmin):
    list_display = ("ats", "board_token", "employer", "is_active", "created_at")
    list_filter = ("ats", "is_active")
    search_fields = ("board_token", "employer__name")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "employer",
        "source_ats",
        "source_job_id",
        "status",
        "is_remote",
        "needs_classification",
        "scraped_at",
    )
    list_filter = ("status", "is_remote", "needs_classification", "source_ats")
    search_fields = ("title", "source_job_id", "employer__name")
    readonly_fields = ("content_hash", "created_at", "updated_at")
