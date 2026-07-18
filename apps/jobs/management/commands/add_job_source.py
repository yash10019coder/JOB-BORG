"""Register one or more Greenhouse boards as JobSources in a single command.

Replaces hand-writing a shell/ORM snippet per company: validates each board
token against the live Greenhouse API before writing anything, so a typo'd
token fails fast instead of creating a dead Employer/JobSource pair.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.employers.models import Employer
from apps.jobs.ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from apps.jobs.ingestion.greenhouse_client import GreenhouseClient
from apps.jobs.models import JobSource


class Command(BaseCommand):
    help = (
        "Register one or more Greenhouse board tokens as JobSources, "
        "creating the Employer if needed. Validates each token against the "
        "live Greenhouse API before writing anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "board_tokens",
            nargs="+",
            help="One or more Greenhouse board tokens, e.g. stripe airbnb figma. "
            "The token is the path segment in boards.greenhouse.io/<token>.",
        )
        parser.add_argument(
            "--name",
            help="Employer display name. Only valid with a single board token; "
            "defaults to the token, title-cased.",
        )

    def handle(self, *args, **options):
        board_tokens = options["board_tokens"]
        name_override = options.get("name")

        if name_override and len(board_tokens) > 1:
            raise CommandError("--name can only be used with a single board token.")

        client = GreenhouseClient()
        for token in board_tokens:
            self._register_one(client, token, name_override)

    def _register_one(self, client, token, name_override):
        try:
            jobs = client.fetch_jobs(token)
        except (GreenhouseUnavailable, GreenhouseParseError) as exc:
            self.stderr.write(self.style.ERROR(f"{token}: unreachable or invalid board ({exc})"))
            return

        if JobSource.objects.filter(ats=JobSource.ATS.GREENHOUSE, board_token=token).exists():
            self.stdout.write(self.style.WARNING(f"{token}: JobSource already registered, skipping"))
            return

        employer_name = name_override or token.replace("-", " ").title()
        employer, _ = Employer.objects.get_or_create(
            slug=token, defaults={"name": employer_name}
        )
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token=token, employer=employer
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{token}: registered ({employer.name}, {len(jobs)} open jobs live now)"
            )
        )
