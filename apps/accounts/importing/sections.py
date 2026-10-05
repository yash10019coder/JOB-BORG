"""Splitting a normalized resume into sections (rules S1-S8).

A heading is recognised either because it is a known section name (any case) or
because it is shouted (ALL CAPS) or ends in a colon. A lone Title-Case line that
is not a known name is *not* a heading: real resumes list one skill or project
per line ("Python", "React", "Android"), and treating those as headings would
cut every section to pieces.
"""
import re
from dataclasses import dataclass

from apps.accounts.importing.documents import bullet_of

SUMMARY, EXPERIENCE, PROJECTS, EDUCATION = "SUMMARY", "EXPERIENCE", "PROJECTS", "EDUCATION"
SKILLS, CERTS, OTHER = "SKILLS", "CERTS", "OTHER"

# S2 (compared lower-case with "&" read as "and" and punctuation removed)
SYNONYMS = {
    SUMMARY: ("summary", "professional summary", "profile", "objective", "career objective",
              "about", "about me", "executive summary"),
    EXPERIENCE: ("experience", "work experience", "professional experience", "employment",
                 "employment history", "work history", "career history", "relevant experience",
                 "internships", "internship experience"),
    PROJECTS: ("projects", "personal projects", "academic projects", "selected projects",
               "key projects", "open source", "open source contributions",
               "open source contribution", "side projects"),
    EDUCATION: ("education", "academic background", "qualifications", "academics",
                "educational qualifications", "academic qualifications"),
    SKILLS: ("skills", "technical skills", "programming skills", "technical proficiency",
             "technical proficiencies", "technical expertise", "key skills", "core competencies",
             "technologies", "tools", "tech stack", "top skills", "skills and tools",
             "skills and technologies", "technical summary"),
    CERTS: ("certifications", "certificates", "certification", "licenses", "licenses and certifications",
            "courses", "training"),
    OTHER: ("achievements", "key achievements", "awards", "honors", "honours", "publications",
            "volunteer", "volunteering", "organizations", "extracurricular", "extracurriculars",
            "extracurricular activities", "interests", "hobbies", "languages", "soft skills",
            "references", "responsibilities", "positions of responsibility"),
}

_NORMALIZE = re.compile(r"[^a-z ]")
_KIND_BY_NAME = {name: kind for kind, names in SYNONYMS.items() for name in names}
_MAX_HEADING_CHARS, _MAX_HEADING_TOKENS = 40, 5


@dataclass(frozen=True)
class Section:
    kind: str
    heading: int  # index of the heading line
    start: int  # first content line
    end: int  # one past the last content line
    known: bool  # False for a shouted/colon heading we do not recognise


def _name(line):
    return " ".join(_NORMALIZE.sub(" ", line.strip(" :").lower().replace("&", " and ")).split())


def heading_kind(line):
    """The canonical section kind for a heading line, ``OTHER`` for a shouted
    or colon-terminated unknown heading, or ``None`` if it is not a heading (S1/S2)."""
    text = line.strip()
    if not text or len(text) > _MAX_HEADING_CHARS or bullet_of(text):
        return None
    if re.search(r"\d", text) or text.endswith((".", ",")):
        return None
    if len(text.split()) > _MAX_HEADING_TOKENS:
        return None
    kind = _KIND_BY_NAME.get(_name(text))
    if kind:
        return kind
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) >= 2 and (text.isupper() or text.endswith(":")):
        return OTHER
    return None


def find_sections(lines, noise=frozenset()):
    """Sections in document order (S3). Repeated header/footer lines are ignored."""
    headings = []
    for index, line in enumerate(lines):
        if index in noise:
            continue
        kind = heading_kind(line)
        if kind:
            headings.append((index, kind, _name(line) in _KIND_BY_NAME))
    sections = []
    for position, (index, kind, known) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.append(Section(kind=kind, heading=index, start=index + 1, end=end, known=known))
    return sections


def section_of(sections, line_index):
    """The section containing a content line, or ``None`` (before any heading)."""
    for section in sections:
        if section.start <= line_index < section.end:
            return section
    return None


def lines_of(sections, kind):
    """Content line indexes of every section of ``kind``, in order (S8: duplicate
    sections are concatenated)."""
    indexes = []
    for section in sections:
        if section.kind == kind:
            indexes.extend(range(section.start, section.end))
    return indexes


def heading_with_content(line):
    """``("SKILLS", "Python, Java")`` for ``"Technical Skills: Python, Java"``: a
    known heading name followed by a colon and content on the same line."""
    match = re.match(r"^([A-Za-z &]{3,40}):\s*(\S.*)$", line.strip())
    if not match:
        return None
    kind = _KIND_BY_NAME.get(_name(match.group(1)))
    return (kind, match.group(2)) if kind else None
