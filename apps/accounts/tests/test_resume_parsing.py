"""Tests for apps.accounts.resume_parsing -- text extraction + the
parse_resume Celery task (see U1 of
docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md).

PDF/DOCX fixtures are built in-memory (no binary fixture files committed) so
the extraction contract stays easy to read and adjust.
"""
import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts.models import Profile
from apps.accounts.resume_parsing import (
    extract_resume_text,
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_txt,
    parse_resume,
)

User = get_user_model()


def make_pdf_bytes(text):
    """Hand-build a minimal single-page PDF containing `text` as a Tj-drawn
    string -- avoids depending on a PDF-writing library the codebase doesn't
    otherwise need."""
    content = f"BT /F1 24 Tf 72 712 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return buf.getvalue()


def make_blank_pdf_bytes():
    """A structurally valid single-page PDF with no text content at all --
    stands in for a scanned-image PDF that has nothing to extract."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 612 792] >>",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return buf.getvalue()


def make_docx_bytes(text):
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


class ExtractTextFromPdfTests(TestCase):
    def test_well_formed_pdf_yields_text(self):
        pdf_bytes = make_pdf_bytes("Senior Backend Engineer, 8 years Python")
        text = extract_text_from_pdf(io.BytesIO(pdf_bytes))
        self.assertIn("Senior Backend Engineer", text)

    def test_pdf_with_no_extractable_text_returns_empty_string(self):
        text = extract_text_from_pdf(io.BytesIO(make_blank_pdf_bytes()))
        self.assertEqual(text, "")

    def test_corrupted_pdf_returns_empty_string_not_exception(self):
        text = extract_text_from_pdf(io.BytesIO(b"not a real pdf at all"))
        self.assertEqual(text, "")


class ExtractTextFromDocxTests(TestCase):
    def test_well_formed_docx_yields_text(self):
        docx_bytes = make_docx_bytes("Platform Engineer with Kubernetes experience")
        text = extract_text_from_docx(io.BytesIO(docx_bytes))
        self.assertIn("Platform Engineer with Kubernetes experience", text)

    def test_corrupted_docx_returns_empty_string_not_exception(self):
        text = extract_text_from_docx(io.BytesIO(b"not a real docx at all"))
        self.assertEqual(text, "")


class ExtractTextFromTxtTests(TestCase):
    def test_plain_text_round_trips(self):
        text = extract_text_from_txt(io.BytesIO(b"Full-stack engineer, remote-only"))
        self.assertEqual(text, "Full-stack engineer, remote-only")


class ExtractResumeTextDispatchTests(TestCase):
    def test_dispatches_by_extension_pdf(self):
        field = SimpleUploadedFile(
            "resume.pdf", make_pdf_bytes("Dispatch check PDF"), content_type="application/pdf"
        )
        self.assertIn("Dispatch check PDF", extract_resume_text(field))

    def test_dispatches_by_extension_docx(self):
        field = SimpleUploadedFile(
            "resume.docx",
            make_docx_bytes("Dispatch check DOCX"),
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
        self.assertIn("Dispatch check DOCX", extract_resume_text(field))

    def test_unsupported_extension_returns_empty_string(self):
        field = SimpleUploadedFile("resume.exe", b"MZ\x90\x00", content_type="application/x-msdownload")
        self.assertEqual(extract_resume_text(field), "")

    def test_no_file_returns_empty_string(self):
        self.assertEqual(extract_resume_text(None), "")


class ParseResumeTaskTests(TestCase):
    """Exercises the parse_resume Celery task directly -- CELERY_TASK_ALWAYS_EAGER
    (config/settings/test.py) means Profile.set_resume()'s .delay() call runs
    synchronously too, but calling the task function keeps these tests
    focused on parsing behavior rather than the enqueue call site."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Actual file writes (FileSystemStorage in test settings, since no
        # AWS_STORAGE_BUCKET_NAME is set) go to an isolated tmp dir rather
        # than the repo's real MEDIA_ROOT.
        cls._media_root = tempfile.mkdtemp(prefix="jobborg-test-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(username="parses", password="pw")
        self.profile = self.user.profile

    def test_pdf_resume_populates_resume_text(self):
        self.profile.resume = SimpleUploadedFile(
            "resume.pdf",
            make_pdf_bytes("Populated from PDF"),
            content_type="application/pdf",
        )
        self.profile.save(update_fields=["resume"])

        parse_resume(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertIn("Populated from PDF", self.profile.resume_text)

    def test_docx_resume_populates_resume_text(self):
        self.profile.resume = SimpleUploadedFile(
            "resume.docx",
            make_docx_bytes("Populated from DOCX"),
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
        self.profile.save(update_fields=["resume"])

        parse_resume(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertIn("Populated from DOCX", self.profile.resume_text)

    def test_resume_with_no_extractable_text_leaves_resume_text_empty(self):
        self.profile.resume = SimpleUploadedFile(
            "resume.pdf", make_blank_pdf_bytes(), content_type="application/pdf"
        )
        self.profile.save(update_fields=["resume"])

        parse_resume(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.resume_text, "")

    def test_no_resume_uploaded_leaves_resume_text_empty(self):
        parse_resume(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.resume_text, "")

    def test_missing_profile_does_not_raise(self):
        parse_resume(self.profile.pk + 999999)  # no such row
