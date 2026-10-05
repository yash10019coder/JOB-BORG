"""Measure the import rules on a folder of real documents (rules E1-E3).

Strictly read-only and aggregate-only: files are opened for reading, nothing is
copied, stored or logged, and the results hold counts and booleans, never text,
never file names. Later units register *probes* (one per field / entry class);
this module only runs whatever is registered, so the same command grows with the
rules.

Not part of CI: the corpus is real personal data and is never committed.
"""
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

from apps.accounts.importing.documents import (
    DocumentError,
    normalize_text,
    read_document,
)

# Refusal codes that, on a folder of ordinary resumes, indicate the safety rules
# are rejecting something they should not (gate: zero false rejections, E2).
SUSPECT_REFUSALS = frozenset(
    {"pdf_active_content", "pdf_unreadable", "docx_macros", "docx_unsafe",
     "docx_too_complex", "docx_too_large", "docx_unreadable", "docx_invalid",
     "txt_unreadable", "not_a_pdf"}
)

# name -> callable(NormalizedText) -> bool | str | None, registered by the rule
# modules as they are built. A bool counts "found"; a str is a class label.
PROBES = {}


def register_probe(name, probe):
    PROBES[name] = probe


def load_default_probes():
    """Register the probes the rule modules provide (idempotent)."""
    from apps.accounts.importing import rules

    for name, probe in rules.PROBES.items():
        register_probe(name, probe)


@dataclass
class DocResult:
    kind: str
    error: str = ""
    pages: int = 0
    chars: int = 0
    lines: int = 0
    truncated: bool = False
    noise_lines: int = 0
    long_lines: int = 0
    seconds: float = 0.0
    probes: dict = field(default_factory=dict)


def iter_files(directory, name_regex=r"(?i)resume|cv", exclude_regex=r"(?i)cover"):
    """Document files under ``directory`` whose names match; sorted, so runs
    are repeatable. Hidden folders (``.claude`` ...) are skipped."""
    include, exclude = re.compile(name_regex), re.compile(exclude_regex)
    found = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            extension = os.path.splitext(name)[1].lower()
            if extension not in (".pdf", ".docx", ".txt") or name.startswith("."):
                continue
            if include.search(name) and not exclude.search(name):
                found.append(os.path.join(root, name))
    return sorted(found)


def evaluate_file(path):
    started = time.monotonic()
    extension = os.path.splitext(path)[1].lower()
    result = DocResult(kind=extension.lstrip("."))
    try:
        with open(path, "rb") as handle:
            document = read_document(handle.read(), extension)
    except DocumentError as exc:
        result.error = exc.code
    except OSError:
        result.error = "unreadable_file"
    else:
        normalized = normalize_text(document.text)
        result.pages = document.pages
        result.chars = len(normalized.text)
        result.lines = len(normalized.lines)
        result.truncated = normalized.truncated
        result.noise_lines = len(normalized.noise)
        result.long_lines = sum(1 for line in normalized.lines if len(line) > 300)
        for name, probe in PROBES.items():
            try:
                result.probes[name] = probe(normalized)
            except Exception:  # noqa: BLE001 -- a probe bug must not stop the run
                result.probes[name] = None
    result.seconds = time.monotonic() - started
    return result


def summarize(results):
    """Aggregate numbers only (no names, no text)."""
    total = len(results)
    read = [r for r in results if not r.error]
    refusals = Counter(r.error for r in results if r.error)
    probes = {}
    for name in PROBES:
        values = [r.probes.get(name) for r in read]
        if any(isinstance(v, str) for v in values):
            probes[name] = dict(Counter(v for v in values if v))
        else:
            probes[name] = sum(1 for v in values if v)
    seconds = sorted(r.seconds for r in results)
    return {
        "files": total,
        "kinds": dict(Counter(r.kind for r in results)),
        "read": len(read),
        "refusals": dict(refusals),
        "suspect_refusals": sum(c for code, c in refusals.items() if code in SUSPECT_REFUSALS),
        "pages_median": statistics.median([r.pages for r in read]) if read else 0,
        "pages_max": max((r.pages for r in read), default=0),
        "chars_median": int(statistics.median([r.chars for r in read])) if read else 0,
        "truncated": sum(r.truncated for r in read),
        "docs_with_long_lines": sum(1 for r in read if r.long_lines),
        "docs_with_noise_lines": sum(1 for r in read if r.noise_lines),
        "seconds_max": round(seconds[-1], 3) if seconds else 0.0,
        "seconds_p95": round(seconds[int(0.95 * (len(seconds) - 1))], 3) if seconds else 0.0,
        "probes": probes,
    }
