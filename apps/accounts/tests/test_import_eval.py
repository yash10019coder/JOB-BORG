"""The local evaluation command (rules E1-E3): aggregate-only, read-only."""
import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.accounts.importing import evaluation
from apps.accounts.tests.test_import_documents import BODY, build_docx, build_pdf

SENTINEL = "ZXQ-SECRET-CONTENT"


class _Dir:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        return self.tmp.name

    def __exit__(self, *exc):
        self.tmp.cleanup()


def write(directory, name, data):
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def run(directory, *args):
    out = StringIO()
    call_command("import_eval", "--dir", directory, *args, stdout=out)
    return out.getvalue()


class ImportEvalTests(SimpleTestCase):
    def test_E1_only_counts_are_printed_never_names_or_content(self):
        secret_lines = BODY + [f"{SENTINEL} personal detail"]
        with _Dir() as d:
            write(d, f"{SENTINEL}_resume.pdf", build_pdf(secret_lines))
            write(d, f"{SENTINEL}_cv.docx", build_docx(secret_lines))
            write(d, f"{SENTINEL}_resume.txt", "\n".join(secret_lines).encode())
            text = run(d)
            as_json = run(d, "--json")
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(SENTINEL, as_json)
        self.assertIn("Files: 3", text)
        self.assertEqual(json.loads(as_json)["read"], 3)

    def test_E1_it_never_modifies_the_folder(self):
        with _Dir() as d:
            path = write(d, "my_resume.pdf", build_pdf())
            before = (os.stat(path).st_mtime_ns, os.listdir(d))
            run(d)
            self.assertEqual((os.stat(path).st_mtime_ns, os.listdir(d)), before)

    def test_refusals_are_counted_by_code_and_the_gate_reports_them(self):
        with _Dir() as d:
            write(d, "ok_resume.pdf", build_pdf())
            write(d, "scan_resume.pdf", build_pdf(["tiny"]))
            write(d, "js_resume.pdf", build_pdf(catalog_extra=b"/OpenAction << /S /JavaScript /JS (x) >>"))
            summary = json.loads(run(d, "--json"))
            text = run(d)
        self.assertEqual(summary["read"], 1)
        self.assertEqual(summary["refusals"], {"no_text_found": 1, "pdf_active_content": 1})
        self.assertEqual(summary["suspect_refusals"], 1)
        self.assertIn("Gate D-rules", text)
        self.assertIn("FAIL", text)

    def test_the_gate_passes_when_only_ordinary_files_are_present(self):
        with _Dir() as d:
            write(d, "a_resume.pdf", build_pdf(catalog_extra=b"/OpenAction [5 0 R /Fit]"))
            write(d, "b_resume.docx", build_docx())
            self.assertIn("PASS", run(d))

    def test_name_filters_extensions_and_hidden_folders(self):
        with _Dir() as d:
            write(d, "jane_resume.pdf", build_pdf())
            write(d, "cover letter resume.pdf", build_pdf())  # excluded by default
            write(d, "notes.pdf", build_pdf())  # name does not match
            write(d, "photo_resume.png", b"x")  # not a document
            write(d, ".hidden/secret_resume.pdf", build_pdf())  # hidden folder
            write(d, "sub/other_cv.pdf", build_pdf())
            summary = json.loads(run(d, "--json"))
            custom = json.loads(run(d, "--json", "--name-regex", "notes", "--exclude-regex", "^$"))
        self.assertEqual(summary["files"], 2)
        self.assertEqual(custom["files"], 1)

    def test_a_missing_folder_or_no_matches_is_a_clear_error(self):
        with self.assertRaises(CommandError):
            run("/nonexistent/folder")
        with _Dir() as d, self.assertRaises(CommandError):
            run(d)

    def test_an_unreadable_file_is_counted_not_fatal(self):
        with _Dir() as d:
            write(d, "ok_resume.pdf", build_pdf())
            path = write(d, "locked_resume.pdf", build_pdf())
            os.chmod(path, 0)
            try:
                summary = json.loads(run(d, "--json"))
            finally:
                os.chmod(path, 0o600)
        if os.geteuid() != 0:  # root can read a chmod 0 file
            self.assertEqual(summary["refusals"], {"unreadable_file": 1})

    def test_registered_probes_are_summarized_and_a_broken_probe_does_not_stop_the_run(self):
        evaluation.register_probe("has_skills_line", lambda n: any("Skills:" in line for line in n.lines))
        evaluation.register_probe("broken", lambda n: 1 / 0)
        evaluation.register_probe("kind", lambda n: "experience" if "Acme" in n.text else "none")
        self.addCleanup(evaluation.PROBES.clear)
        with _Dir() as d:
            write(d, "a_resume.pdf", build_pdf())
            write(d, "b_resume.pdf", build_pdf(["x " * 250]))
            summary = json.loads(run(d, "--json"))
        self.assertEqual(summary["probes"]["has_skills_line"], 1)
        self.assertEqual(summary["probes"]["broken"], 0)
        self.assertEqual(summary["probes"]["kind"], {"experience": 1, "none": 1})
