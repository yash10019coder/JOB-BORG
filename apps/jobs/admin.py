from django.contrib import admin, messages
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.employers.models import Employer

from .ingestion.exceptions import IngestionParseError, IngestionUnavailable
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
        match = Employer.objects.filter(name__icontains=name_fragment).first()
        return f"⚠ {match.name}" if match else ""

    @admin.action(description="Approve selected discovered boards")
    def approve(self, request, queryset):
        approved = 0
        for board in queryset.filter(status=DiscoveredBoard.Status.PENDING):
            try:
                # A savepoint, not just a try/except: a failed INSERT (e.g. a
                # concurrent approval of the same token racing us to
                # uniq_jobsource_ats_board_token) poisons the enclosing
                # transaction for every later query until rolled back --
                # atomic() here means one row's registration failure can't
                # take the rest of this bulk action down with it, mirroring
                # apps.jobs.tasks.discover_boards's own per-token savepoint.
                with transaction.atomic():
                    outcome = register_job_source(
                        board.board_token,
                        employer_name=board.derived_employer_name,
                        ats=board.ats,
                    )
            except (IngestionUnavailable, IngestionParseError) as exc:
                self.message_user(
                    request,
                    f"{board.board_token}: could not approve, re-fetch failed ({exc})",
                    level=messages.ERROR,
                )
                continue
            except IntegrityError as exc:
                self.message_user(
                    request,
                    f"{board.board_token}: could not approve, registration "
                    f"conflicted with an existing JobSource ({exc})",
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

            # Conditional on the row still being PENDING at write time (not
            # just at the queryset filter above) so a concurrent reject() (or
            # a second concurrent approve()) that mutated this row in the
            # window since we read it can't be silently clobbered.
            updated_rows = DiscoveredBoard.objects.filter(
                pk=board.pk, status=DiscoveredBoard.Status.PENDING
            ).update(
                status=DiscoveredBoard.Status.APPROVED,
                reviewed_at=timezone.now(),
                updated_at=timezone.now(),
            )
            if updated_rows:
                approved += 1
            else:
                self.message_user(
                    request,
                    f"{board.board_token}: already actioned by another request "
                    "concurrently -- skipped.",
                    level=messages.WARNING,
                )

        if approved:
            self.message_user(request, f"Approved {approved} board(s).")

    @admin.action(description="Reject selected discovered boards")
    def reject(self, request, queryset):
        updated = queryset.filter(status=DiscoveredBoard.Status.PENDING).update(
            status=DiscoveredBoard.Status.REJECTED,
            reviewed_at=timezone.now(),
            updated_at=timezone.now(),
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
    list_filter = ("status", "is_remote", "needs_classification", "source_ats", "location_resolved")
    search_fields = ("title", "source_job_id", "employer__name")
    readonly_fields = ("content_hash", "created_at", "updated_at")
