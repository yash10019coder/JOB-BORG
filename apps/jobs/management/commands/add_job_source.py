"""Register one or more Greenhouse boards as JobSources in a single command.

Replaces hand-writing a shell/ORM snippet per company: validates each board
token against the live Greenhouse API before writing anything, so a typo'd
token fails fast instead of creating a dead Employer/JobSource pair.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.jobs.ingestion.exceptions import GreenhouseParseError, GreenhouseUnavailable
from apps.jobs.ingestion.greenhouse_client import GreenhouseClient
from apps.jobs.ingestion.register import register_job_source


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
            outcome = register_job_source(token, name_override, client=client)
        except (GreenhouseUnavailable, GreenhouseParseError) as exc:
            self.stderr.write(self.style.ERROR(f"{token}: unreachable or invalid board ({exc})"))
            return

        if outcome.status == "already_registered":
            self.stdout.write(self.style.WARNING(f"{token}: JobSource already registered, skipping"))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"{token}: registered ({outcome.employer.name}, {outcome.job_count} open jobs live now)"
            )
        )
