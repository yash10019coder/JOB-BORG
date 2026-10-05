"""Rule-based extraction of profile fields from normalized resume text.

Implements rules FN (name), FP (phone), FU (links), FL (location), SK (declared
skills), FK (skills -> tags) and the provisional LinkedIn-export layout (S9-S10).
Every value is taken from a span of the text and re-derived by
:func:`grounding.is_grounded`; anything that does not survive is dropped, never
repaired or guessed. Entries (experience/projects) live in ``entries.py``.

Pure: no database, no network. Regexes are stdlib ``re`` over input already
capped by ``normalize_text``; line-based rules skip lines over 300 characters
(N6), skill lines are bounded separately.
"""
import re
from dataclasses import dataclass

from apps.accounts.importing import sections as sec
from apps.accounts.importing import skills_lexicon as lex
from apps.accounts.importing.documents import NormalizedText, bullet_of, strip_bullet
from apps.accounts.importing.grounding import (
    Proposal,
    derive_location,
    derive_name,
    is_grounded,
    make_snippet,
    tag_for_skill,
)
from apps.accounts.importing.validators import (
    ImportValueError,
    clean_phone,
    clean_text,
    clean_url,
)

MAX_LINE = 300  # N6: heading, anchor, contact and name rules skip longer lines
MAX_SKILL_LINE = 2000
NAME_LINES, PHONE_LINES, ZONE_LINES = 3, 15, 10
MAX_DECLARED_SKILLS = 120
MAX_TAGS = 50

_NAME_SUFFIX_TAIL = re.compile(
    r"^\s*(?:ph\.?d|m\.?b\.?a|m\.?d|c\.?p\.?a|p\.?m\.?p|jr|sr|ii|iii|iv|b\.?tech|m\.?tech)\b", re.I
)
_CUT = re.compile(r"\s[:|•·–—/]\s|[|•·@:/\d]|\s-\s")
_COMMA_CUT_TAIL = re.compile(r"@|\d|https?://|www\.", re.I)
_PHONE = re.compile(r"(?<![\d/])\(?\+?\(?\d[\d ()./-]{5,20}\d(?![\d/])")
_SHORT_WORD_END = re.compile(r"(?<![^\W\d_])[^\W\d_]{1,8}$")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_PHONE_GLUED_LABEL = re.compile(r"(?:phone|hone|mobile|obile|mob|tel|cell|call|contact|ph)$", re.I)
_PHONE_LABEL = re.compile(r"(?:phone|mobile|mob|tel|cell|contact|ph|m|t|p)\s*[:.\-]?\s*$", re.I)
_LINKEDIN = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:(?:www|[a-z]{2,3})\.)?linkedin\.com/in/[A-Za-z0-9_%\-]{3,100}", re.I
)
_GITHUB = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/?"
    r"(?![A-Za-z0-9_/-])(?!\.[A-Za-z0-9])",
    re.I,
)
_BARE_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}(?:/\S*)?$", re.I)
# Degree and title abbreviations that look like a domain ("B.Tech", "M.Sc", "Ph.D").
_DEGREE_ABBREVIATIONS = frozenset(
    "b.tech m.tech b.sc m.sc b.e m.e b.a m.a b.s m.s b.com m.com b.ed m.ed ph.d b.arch m.arch "
    "b.des m.des b.pharm m.pharm b.ca m.ca e.g i.e u.s u.k a.i".split()
)
_FILE_EXTENSIONS = frozenset(
    "pdf doc docx png jpg jpeg gif txt py js ts java html css json xml zip md csv rtf cpp rb go rs".split()
)
_PORTFOLIO_LABEL = re.compile(
    r"^(?:portfolio|website|web\s*site|blog|personal\s+(?:site|website)|web)\s*[:\-]\s*(\S+)", re.I
)
_PLACE = re.compile(r"^[A-Z][A-Za-z.'’ -]+(?:,\s*[A-Za-z.'’ -]{2,}){1,2}$")
_STOP_SKILL_WORDS = frozenset(
    "and etc others more proficient experienced basic intermediate advanced familiar knowledge "
    "good strong excellent working hands-on handson".split()
)
_PROFICIENCY = re.compile(
    r"\s*(?:[-–:]\s*)?(?:\((?:beginner|intermediate|advanced|expert|proficient|basic|familiar)\)|"
    r"[-–]\s*(?:beginner|intermediate|advanced|expert|proficient|basic|familiar))\s*$",
    re.I,
)
_YEARS_PAREN = re.compile(r"\s*\([^()]*\d[^()]*\)\s*$")


@dataclass(frozen=True)
class Doc:
    text: str
    lines: tuple
    offsets: tuple
    noise: frozenset
    sections: tuple

    def content(self):
        """Indexes of non-blank lines that are not repeated headers/footers."""
        return [i for i, line in enumerate(self.lines) if line and i not in self.noise]

    def head(self, count):
        return self.content()[:count]

    def at(self, index, start, end):
        base = self.offsets[index]
        return (base + start, base + end)


def prepare(normalized):
    """A :class:`Doc` view of :func:`documents.normalize_text` output."""
    offsets, position = [], 0
    for line in normalized.lines:
        offsets.append(position)
        position += len(line) + 1
    return Doc(
        text=normalized.text,
        lines=normalized.lines,
        offsets=tuple(offsets),
        noise=normalized.noise,
        sections=tuple(sec.find_sections(normalized.lines, normalized.noise)),
    )


def _proposal(doc, field, value, span, *, provisional=False, extractor="rule"):
    return Proposal(
        field=field,
        value=value,
        spans=(span,),
        snippet=make_snippet(doc.text, span[0], span[1]),
        extractor=extractor,
        provisional=provisional,
    )


# --------------------------------------------------------------------------
# Name (FN1-FN6)
# --------------------------------------------------------------------------
def extract_name(doc):
    for index in doc.head(NAME_LINES):
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        cut = _CUT.search(line)
        prefix = line[: cut.start()] if cut else line
        comma = prefix.find(",")
        if comma != -1:  # "Jane Doe, PhD" is a name; "New Delhi, India" is not
            tail = prefix[comma + 1:]
            prefix = prefix[:comma] if _NAME_SUFFIX_TAIL.match(tail) else prefix
        if prefix.strip():
            try:
                value = derive_name(prefix)
            except ImportValueError:
                value = None
            if value:
                return _proposal(doc, "full_name", value, doc.at(index, 0, len(prefix)))
        if _COMMA_CUT_TAIL.search(line):  # the contact block started: stop looking
            break
    return None


# --------------------------------------------------------------------------
# Phone (FP1-FP4)
# --------------------------------------------------------------------------
def raw_starts_international(text):
    return text.lstrip("(").startswith("+")


def _token_around(line, start, end):
    """The whitespace/``|•·,;``-delimited token containing ``line[start:end]``."""
    left = max((line.rfind(ch, 0, start) for ch in " \t|•·,;"), default=-1) + 1
    right_positions = [p for p in (line.find(ch, end) for ch in " \t|•·,;") if p != -1]
    return line[left: min(right_positions) if right_positions else len(line)]


def extract_phone(doc):
    candidates = []
    for index in doc.head(PHONE_LINES):
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        for match in _PHONE.finditer(line):
            start, end = match.span()
            before = line[:start]
            # Icon fonts glue the label (or its tail: "ne+91 98...") onto an
            # international number. A "+" number never occurs inside a handle or a
            # URL slug, so a short word directly before it is a label, not a handle.
            international = raw_starts_international(match.group())
            glued_label = bool(_PHONE_GLUED_LABEL.search(before)) or (
                international and bool(_SHORT_WORD_END.search(before))
            )
            if (start and line[start - 1].isalpha() and not glued_label) or (
                end < len(line) and line[end].isalpha()
            ):
                continue
            token = _token_around(line, start, end)
            if re.search(r"@|://|www\.|\.com|\.in/|\.io/", token, re.I):
                continue  # a number inside an e-mail address, URL or handle
            raw = match.group().strip()
            if len(_YEAR.findall(raw)) >= 2:  # two date ranges run together, not a number
                continue
            try:
                clean_phone(raw)
            except ImportValueError:
                continue
            labelled = glued_label or bool(_PHONE_LABEL.search(before))
            candidates.append((not labelled, len(candidates), index, start, start + len(match.group().rstrip())))
    if not candidates:
        return None
    _, _, index, start, end = min(candidates)
    raw = doc.lines[index][start:end].strip()
    return _proposal(doc, "phone", clean_phone(raw), doc.at(index, start, start + len(raw)))


# --------------------------------------------------------------------------
# Links (FU1-FU7)
# --------------------------------------------------------------------------
def _first_link(doc, pattern, field, kind):
    for index in doc.content():
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        for match in pattern.finditer(line):
            raw = match.group()
            try:
                value = clean_url(raw, kind=kind)
            except ImportValueError:
                continue
            return _proposal(doc, field, value, doc.at(index, match.start(), match.end()))
    return None


def extract_linkedin(doc):
    return _first_link(doc, _LINKEDIN, "linkedin_url", "linkedin")


def extract_github(doc):
    return _first_link(doc, _GITHUB, "github_url", "github")


def _portfolio_ok(token):
    host = re.sub(r"^https?://", "", token, flags=re.I).split("/")[0].lower()
    if host in lex.DOTTED_TECH or token.casefold() in lex.DOTTED_TECH:
        return False
    if host.startswith("www."):
        host = host[4:]
    if host in ("linkedin.com", "github.com") or host.endswith(".linkedin.com"):
        return False
    return True


def extract_portfolio(doc):
    """FU3: an explicit https link or a bare domain in the contact zone; outside
    the zone only a line labelled Portfolio/Website/Blog counts."""
    for index in doc.head(ZONE_LINES):
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        for match in re.finditer(r"[^\s|•·;,]+", line):
            token = match.group().strip("()[]<>\"'").rstrip(".,;:")
            if not token or "@" in token or token.endswith(":"):
                continue
            explicit = token.lower().startswith("https://")
            if not explicit and not _BARE_DOMAIN.match(token):
                continue
            if not explicit:
                host = token.split("/")[0].lower()
                if (
                    host.rsplit(".", 1)[-1] in _FILE_EXTENSIONS
                    or host in _DEGREE_ABBREVIATIONS
                    or len(host.split(".")[0]) < 3  # "b.tech", "m.sc": real domains rarely start so short
                ):
                    continue
            if not _portfolio_ok(token):
                continue
            try:
                value = clean_url(token)
            except ImportValueError:
                continue
            offset = match.start() + match.group().find(token)
            return _proposal(doc, "portfolio_url", value, doc.at(index, offset, offset + len(token)))
    for index in doc.content():
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        labelled = _PORTFOLIO_LABEL.match(line)
        if not labelled:
            continue
        token = labelled.group(1).strip("()[]<>\"'").rstrip(".,;:")
        if "@" in token or not _portfolio_ok(token):
            continue
        try:
            value = clean_url(token)
        except ImportValueError:
            continue
        offset = labelled.start(1) + labelled.group(1).find(token)
        return _proposal(doc, "portfolio_url", value, doc.at(index, offset, offset + len(token)))
    return None


# --------------------------------------------------------------------------
# Location (FL1-FL4)
# --------------------------------------------------------------------------
def extract_location(doc):
    """City (and country) from a ``City, Region[, Country]`` token in the contact
    zone. Never from an employer line, a phone code or "Remote" (FL3)."""
    for index in doc.head(ZONE_LINES):
        line = doc.lines[index]
        if len(line) > MAX_LINE:
            continue
        for piece in re.finditer(r"[^|•·;]+", line):
            token = piece.group().strip()
            if not token or re.search(r"[\d@]|https?://", token) or not _PLACE.match(token):
                continue
            try:
                city, alpha3 = derive_location(token)
            except ImportValueError:
                continue
            start = piece.start() + piece.group().find(token)
            span = doc.at(index, start, start + len(token))
            found = [_proposal(doc, "location_city", city, span)]
            if alpha3:
                found.append(_proposal(doc, "location_country", alpha3, span))
            return found
    return []


# --------------------------------------------------------------------------
# Declared skills (SK1-SK7) and tags (FK1-FK3)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DeclaredSkill:
    name: str  # canonical spelling (lexicon) or the resume's own
    raw: str
    start: int
    end: int


def _category_segments(line):
    """SK2: split a skills line into item segments, discarding ``Label:`` words.
    Labels may be glued onto the previous items without a separator."""
    colons = [
        m.start()
        for m in re.finditer(":", line)
        if line[m.start() + 1: m.start() + 3] != "//" and not line[m.start() + 1: m.start() + 2].isdigit()
    ]
    cuts = []
    for colon in colons:
        before = line[:colon]
        seg_start = max(before.rfind(ch) for ch in ",;|•·") + 1
        words = list(re.finditer(r"\S+", before[seg_start:]))[-4:]
        while len(words) > 1 and (lex.canonical_skill(words[0].group()) or words[0].group()[0].islower()):
            words = words[1:]
        if not words or not (words[0].group()[0].isupper() or words[0].group()[0] in "&("):
            continue
        cuts.append((seg_start + words[0].start(), colon + 1))
    segments, position = [], 0
    for label_start, colon_end in cuts:
        if label_start >= position:
            segments.append((line[position:label_start], position))
        position = max(position, colon_end)
    segments.append((line[position:], position))
    return [(text, offset) for text, offset in segments if text.strip()]


def _split_top_level(text, base):
    """Split on ``, ; | • ·`` outside parentheses; yields (piece, absolute offset)."""
    pieces, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char in ",;|•·" and depth == 0:
            pieces.append((text[start:index], base + start))
            start = index + 1
    pieces.append((text[start:], base + start))
    return pieces


def _acceptable_skill(item):
    if not 1 <= len(item) <= 40 or len(item.split()) > 4:
        return False
    if not any(c.isalpha() for c in item) or item.casefold() in _STOP_SKILL_WORDS:
        return False
    if re.search(r"@|://|\. |\.$", item) and not lex.canonical_skill(item):
        return False
    return True


def _inner_items(inner, base):
    """Comma-separated parts of a parenthetical -> [(text, absolute offset)]."""
    items, position = [], 0
    for part in inner.split(","):
        text = part.strip()
        if text:
            items.append((text, base + position + (len(part) - len(part.lstrip()))))
        position += len(part) + 1
    return items


def _items_of(piece, offset):
    """SK4-SK6 for one comma-separated piece -> [(raw, absolute start, end)]."""
    lead = len(piece) - len(piece.lstrip())
    text = piece.strip()
    start = offset + lead
    if bullet_of(text):
        stripped = strip_bullet(text)
        start += len(text) - len(stripped)
        text = stripped
    if not text:
        return []
    text = _YEARS_PAREN.sub("", _PROFICIENCY.sub("", text)).strip()
    group = re.match(r"^(.*?)\s*\(([^()]*)\)$", text)
    if group and group.group(2).strip():
        outer = group.group(1).strip()
        inner = _inner_items(group.group(2), start + group.start(2))
        known = [(t, s) for t, s in inner if lex.canonical_skill(t)]
        out = []
        # "Group (a, b)": keep the group only if it is itself a skill, or if
        # nothing inside is (SK4); a lone parenthetical names a skill only if known.
        if outer and (lex.canonical_skill(outer) or not (known or len(inner) > 1)):
            out.append((outer, start))
        out.extend(known or (inner if len(inner) > 1 else []))
        return [(t, s, s + len(t)) for t, s in out if _acceptable_skill(t)]
    text = text.rstrip(".") if lex.canonical_skill(text.rstrip(".")) else text
    return [(text, start, start + len(text))] if _acceptable_skill(text) else []


def declared_skills(doc):
    """The resume's own Skills-section items, canonicalised and deduplicated (SK1-SK7)."""
    skill_lines = [(i, 0) for i in sec.lines_of(doc.sections, sec.SKILLS) if i not in doc.noise]
    for index, line in enumerate(doc.lines):
        if index in doc.noise or len(line) > MAX_SKILL_LINE:
            continue
        inline = sec.heading_with_content(line)  # "Technical Skills: Python, Java"
        if inline and inline[0] == sec.SKILLS:
            skill_lines.append((index, len(line) - len(inline[1])))
    found, seen = [], set()
    for index, offset in skill_lines:
        line = doc.lines[index]
        if not line or len(line) > MAX_SKILL_LINE:
            continue
        base = doc.offsets[index]
        for segment, seg_offset in _category_segments(line[offset:]):
            for piece, piece_offset in _split_top_level(segment, offset + seg_offset):
                for raw, start, end in _items_of(piece, piece_offset):
                    name = lex.canonical_skill(raw) or re.sub(r"\s+", " ", raw)
                    if name.casefold() in seen:
                        continue
                    seen.add(name.casefold())
                    found.append(DeclaredSkill(name=name, raw=raw, start=base + start, end=base + end))
                    if len(found) >= MAX_DECLARED_SKILLS:
                        return found
    return found


def extract_tags(doc, skills=None):
    """FK1-FK3: declared skills that map to a live, non-role job-matching tag."""
    tags, spans = [], []
    for skill in skills if skills is not None else declared_skills(doc):
        tag = tag_for_skill(skill.raw)
        if tag and tag not in tags:
            tags.append(tag)
            spans.append((skill.start, skill.end))
        if len(tags) >= MAX_TAGS:
            break
    if not tags:
        return None
    return Proposal(
        field="target_tags",
        value=tags,
        spans=tuple(spans),
        snippet=make_snippet(doc.text, spans[0][0], spans[0][1]),
    )


# --------------------------------------------------------------------------
# LinkedIn export layout (S9-S10, PROVISIONAL: no real export in the sample)
# --------------------------------------------------------------------------
def is_linkedin_export(doc):
    head = [line.strip().casefold() for line in doc.lines[:60]]
    return (
        "contact" in head
        and "top skills" in head
        and bool(_LINKEDIN.search("\n".join(doc.lines[:60])))
    )


def extract_linkedin_header(doc):
    """Name, headline and location from the three lines that precede the first
    main-column heading (S10). Provisional: every result defaults to reject."""
    main = None
    for section in doc.sections:
        if section.kind in (sec.SUMMARY, sec.EXPERIENCE, sec.EDUCATION):
            main = section.heading
            break
    if main is None:
        return []
    before = [i for i in range(main) if doc.lines[i] and i not in doc.noise][-3:]
    if len(before) < 3:
        return []
    name_i, headline_i, place_i = before
    found = []
    try:
        name = derive_name(doc.lines[name_i])
        found.append(_proposal(doc, "full_name", name, doc.at(name_i, 0, len(doc.lines[name_i])), provisional=True))
    except ImportValueError:
        return []
    try:
        headline = clean_text(doc.lines[headline_i], max_len=255, min_len=3)
        found.append(_proposal(doc, "headline", headline, doc.at(headline_i, 0, len(doc.lines[headline_i])), provisional=True))
    except ImportValueError:
        pass
    try:
        city, alpha3 = derive_location(doc.lines[place_i])
        span = doc.at(place_i, 0, len(doc.lines[place_i]))
        found.append(_proposal(doc, "location_city", city, span, provisional=True))
        if alpha3:
            found.append(_proposal(doc, "location_country", alpha3, span, provisional=True))
    except ImportValueError:
        pass
    return found


# --------------------------------------------------------------------------
# Everything together
# --------------------------------------------------------------------------
def extract_fields(normalized):
    """All field proposals for a document; ungrounded ones are dropped (G1)."""
    if not isinstance(normalized, NormalizedText):
        raise TypeError("extract_fields expects documents.normalize_text() output")
    doc = prepare(normalized)
    skills = declared_skills(doc)
    candidates = []
    if is_linkedin_export(doc):
        candidates.extend(extract_linkedin_header(doc))
    candidates.append(extract_name(doc))
    candidates.append(extract_phone(doc))
    candidates.append(extract_linkedin(doc))
    candidates.append(extract_github(doc))
    candidates.append(extract_portfolio(doc))
    candidates.extend(extract_location(doc))
    candidates.append(extract_tags(doc, skills))
    proposals, taken = [], set()
    for proposal in candidates:
        if proposal is None or proposal.field in taken or not is_grounded(doc.text, proposal):
            continue
        taken.add(proposal.field)
        proposals.append(proposal)
    return proposals


# --------------------------------------------------------------------------
# Probes for ``manage.py import_eval`` (aggregate counts only)
# --------------------------------------------------------------------------
_memo = {"key": None, "fields": frozenset(), "skills": 0}


def _fields_of(normalized):
    if _memo["key"] is not normalized:
        _memo["key"] = normalized
        _memo["fields"] = frozenset(p.field for p in extract_fields(normalized))
        _memo["skills"] = len(declared_skills(prepare(normalized)))
    return _memo["fields"]


PROBES = {
    **{name: (lambda n, name=name: name in _fields_of(n)) for name in (
        "full_name", "phone", "linkedin_url", "github_url", "portfolio_url",
        "location_city", "location_country", "target_tags", "headline",
    )},
    "declared_skills": lambda n: (_fields_of(n), _memo["skills"])[1] > 0,
    "linkedin_export": lambda n: is_linkedin_export(prepare(n)),
}
