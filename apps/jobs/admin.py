from django.contrib import admin, messages
from django.utils import timezone

from apps.employers.models import Employer

from .ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from .ingestion.register import register_job_source
from .models import DiscoveredBoard, Job, JobSource


@admin.register(JobSource)
class JobSourceAdmin(admin.ModelAdmin):
    list_display = ("ats", "board_token", "employer", "is_active", "created_at")
    list_filter = ("ats", "is_active")
    search_fields = ("board_token", "employer__name")


@admin.register(DiscoveredBoard)
class DiscoveredBoardAdmin(admin.ModelAdmin):
    list_display = (
        "ats",
        "board_token",
        "derived_employer_name",
        "status",
        "discovered_at",
        "similar_employer_hint",
    )
    list_filter = ("ats", "status")
    search_fields = ("board_token", "derived_employer_name")
    readonly_fields = ("discovered_at", "reviewed_at", "created_at", "updated_at")
    actions = ["approve", "reject"]

    @admin.display(description="Similar existing employer?")
    def similar_employer_hint(self, obj):
        """A partial, low-recall identity signal -- catches some typosquats
        (a case-insensitive substring match), not all. Structurally
        different tokens (a transposed or abbreviated name) produce no hit.
        This does not resolve the spoofed-board concern; it's one input to
        the reviewer's own judgment, not a verdict.
        """
        name_fragment = obj.derived_employer_name.strip()
        if not name_fragment:
            return ""
        match = (
            Employer.objects.filter(name__icontains=name_fragment)
            .exclude(slug=obj.board_token)
            .first()
        )
        return f"⚠ {match.name}" if match else ""

    @admin.action(description="Approve selected discovered boards")
    def approve(self, request, queryset):
        approved = 0
        for board in queryset.filter(status=DiscoveredBoard.Status.PENDING):
            try:
                outcome = register_job_source(
                    board.board_token, employer_name=board.derived_employer_name
                )
            except (GreenhouseUnavailable, GreenhouseParseError) as exc:
                self.message_user(
                    request,
                    f"{board.board_token}: could not approve, re-fetch failed ({exc})",
                    level=messages.ERROR,
                )
                continue

            if outcome.job_count != board.discovered_job_count:
                self.message_user(
                    request,
                    f"{board.board_token}: job count changed since discovery "
                    f"({board.discovered_job_count} → {outcome.job_count}) "
                    "-- approved anyway, but double-check the board.",
                    level=messages.WARNING,
                )

            board.status = DiscoveredBoard.Status.APPROVED
            board.reviewed_at = timezone.now()
            board.save(update_fields=["status", "reviewed_at", "updated_at"])
            approved += 1

        if approved:
            self.message_user(request, f"Approved {approved} board(s).")

    @admin.action(description="Reject selected discovered boards")
    def reject(self, request, queryset):
        updated = queryset.filter(status=DiscoveredBoard.Status.PENDING).update(
            status=DiscoveredBoard.Status.REJECTED, reviewed_at=timezone.now()
        )
        if updated:
            self.message_user(request, f"Rejected {updated} board(s).")


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
