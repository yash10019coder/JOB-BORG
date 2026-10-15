"""Report what the consensus learner would do and how accurate it has been.

    manage.py learning_report                 # shadow precision per question
    manage.py learning_report --learn-dry-run # also: what the learner would write now
    manage.py learning_report --user alice

Read-only: the dry run never writes.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Profile
from apps.accounts.services.learning import learn_for_profile
from apps.accounts.services.shadow_metrics import candidate_stats, shadow_stats


class Command(BaseCommand):
    help = "Shadow precision of learned answers, and an optional learner dry run."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Limit to one username.")
        parser.add_argument(
            "--learn-dry-run", action="store_true",
            help="Also run the learner in dry-run mode and list what it would write.",
        )

    def handle(self, *args, **options):
        profiles = Profile.objects.select_related("user")
        if options["user"]:
            profiles = profiles.filter(user__username=options["user"])
            if not profiles.exists():
                raise CommandError(f"No such user: {options['user']}")

        self.stdout.write("Shadow precision (learned prefill vs submitted)")
        rows = {}
        for profile in profiles:
            for key, stats in shadow_stats(profile).items():
                rows[(profile.user.username, key)] = stats
        if not rows:
            self.stdout.write("  no observations with a learned prefill yet")
        for (username, key), stats in sorted(rows.items()):
            self.stdout.write(
                f"  {username}  {key[:60]!r}: n={stats.observations} "
                f"precision={stats.precision:.2f} lower_bound={stats.lower_bound:.2f} "
                f"bulk_confirmed={stats.bulk_confirmed}"
            )

        self.stdout.write("Reworded-question candidates (counts per tier; typed or changed answers only)")
        totals = {}
        for profile in profiles:
            for tier, stats in candidate_stats(profile).items():
                total = totals.setdefault(tier, type(stats)(tier))
                for field in ("shown", "accepted", "typed", "matched", "self_reinforced"):
                    setattr(total, field, getattr(total, field) + getattr(stats, field))
        if not totals:
            self.stdout.write("  no candidates shown yet")
        for tier, stats in sorted(totals.items()):
            precision = "n/a" if stats.precision is None else f"{stats.precision:.2f}"
            accept = "n/a" if stats.accept_rate is None else f"{stats.accept_rate:.2f}"
            self.stdout.write(
                f"  {tier}: shown={stats.shown} accepted={stats.accepted} typed={stats.typed} "
                f"matched={stats.matched} precision={precision} "
                f"lower_bound={stats.lower_bound:.2f} accept_rate={accept} "
                f"self_reinforced_excluded={stats.self_reinforced}"
            )

        if options["learn_dry_run"]:
            self.stdout.write("Learner dry run")
            for profile in profiles:
                report = learn_for_profile(profile, dry_run=True)
                self.stdout.write(
                    f"  {profile.user.username}: would write {len(report.written)}, "
                    f"withdraw {len(report.withdrawn)}, suggest {len(report.suggested)}, "
                    f"expire {report.expired}; skipped {report.skipped}"
                )
                for key in report.written:
                    self.stdout.write(f"    + {key[:70]!r}")
