"""Measure the import rules on a folder of real resumes (rules E1-E3).

    manage.py import_eval --dir /path/to/resumes
    manage.py import_eval --dir /path --name-regex '(?i)resume|cv' --json

Read-only and aggregate-only: it prints counts, never file names or text, writes
nothing and is not part of CI (the folder is real personal data).
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.importing import evaluation


class Command(BaseCommand):
    help = "Run the import pipeline over a folder and print aggregate metrics only."

    def add_arguments(self, parser):
        parser.add_argument("--dir", required=True, help="Folder of PDF/DOCX/TXT resumes.")
        parser.add_argument("--name-regex", default=r"(?i)resume|cv", help="Only files whose name matches.")
        parser.add_argument("--exclude-regex", default=r"(?i)cover", help="Skip files whose name matches.")
        parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")

    def handle(self, *args, **options):
        directory = options["dir"]
        if not os.path.isdir(directory):
            raise CommandError("--dir is not a directory")
        files = evaluation.iter_files(directory, options["name_regex"], options["exclude_regex"])
        if not files:
            raise CommandError("no matching files")
        summary = evaluation.summarize([evaluation.evaluate_file(path) for path in files])

        if options["json"]:
            self.stdout.write(json.dumps(summary, indent=2, sort_keys=True))
            return

        out = self.stdout.write
        out(f"Files: {summary['files']}  {summary['kinds']}")
        out(f"Read OK: {summary['read']}   Refused: {summary['files'] - summary['read']}  {summary['refusals'] or ''}")
        out(
            f"Pages median/max: {summary['pages_median']}/{summary['pages_max']}   "
            f"chars median: {summary['chars_median']}   truncated: {summary['truncated']}"
        )
        out(
            f"Docs with lines > 300 chars: {summary['docs_with_long_lines']}   "
            f"with repeated header/footer lines: {summary['docs_with_noise_lines']}"
        )
        out(f"Seconds p95/max per file: {summary['seconds_p95']}/{summary['seconds_max']}")
        for name, value in sorted(summary["probes"].items()):
            out(f"  {name}: {value}")
        verdict = "PASS" if summary["suspect_refusals"] == 0 else "FAIL"
        out(f"Gate D-rules (zero false rejections of ordinary resumes): {verdict} ({summary['suspect_refusals']})")
