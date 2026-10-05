"""Rule-based extraction of experience and project entries (rules EX1-EX16, EA1-EA5).

An *anchor* is a date range inside the Experience (or Projects) section. The
title and the employer are found by classifying the text around the anchor (the
anchor line split on separators, the one or two lines above it, the line below)
as TITLE, ORG, LOC or DETAIL, never by a fixed position: real resumes put the
role and the employer on neighbouring lines in at least four different ways.
When a document is ambiguous for some entries but resolved for others, the
layout of the resolved ones is applied to the rest (flagged, and still default
reject). A skill is attached to an entry only if it appears verbatim in that
entry's own text.

Everything is grounded: each entry carries the spans its title, employer, dates
and skills were cut from, and :func:`entry_grounded` re-derives them.
"""
import dataclasses
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from apps.accounts.importing import rules
from apps.accounts.importing import sections as sec
from apps.accounts.importing import skills_lexicon as lex
from apps.accounts.importing.documents import bullet_of, strip_bullet
from apps.accounts.importing.grounding import make_snippet
from apps.accounts.importing.titles import has_title_word
from apps.accounts.importing.validators import ImportValueError, clean_text
from apps.accounts.services.resume_facts import dates_plausible

TITLE, ORG, LOC, DETAIL = "TITLE", "ORG", "LOC", "DETAIL"
MAX_LINE = 300
MAX_BLOCK_LINES = 40
MAX_EXPERIENCE, MAX_PROJECTS, MAX_ENTRY_SKILLS = 25, 15, 30
MAX_HEAD_TOKENS = 12
LOOKAROUND = 2  # lines examined above an anchor

_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DATE = (
    rf"(?:\d{{1,2}}\s+{_MON}\s+\d{{4}}|{_MON}\s*,?\s*(?:\d{{4}}|['’]\d{{2}})|"
    rf"\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})"
)
_END = rf"(?:{_DATE}|present|current|now|ongoing|till\s+date|to\s+date|today)"
_RANGE = re.compile(
    rf"(?<![\w/])({_DATE})\s*(?:–|—|‐|-|to|until)\s*({_END})(?![\w/])", re.IGNORECASE
)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_CURRENT = re.compile(r"^(?:present|current|now|ongoing|till\s+date|to\s+date|today)$", re.I)
_DEGREE = re.compile(
    r"\b(?:b\.?\s?tech|b\.?\s?e\b|m\.?\s?tech|m\.?\s?e\b|b\.?\s?sc|m\.?\s?sc|b\.?\s?s\b|m\.?\s?s\b|"
    r"mba|bachelor|master|ph\.?d|diploma|cgpa|gpa|b\.?\s?a\b|m\.?\s?a\b|high school|secondary school|"
    r"intermediate|class\s+(?:x|xii|10|12))",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"\s*\|\s*|\s+[-–—]\s*|\s+@\s+|\s+at\s+", re.IGNORECASE)
_REMOTE_TAIL = re.compile(r"[\s,(\[-]*\b(?:remote|hybrid|on-?site|work from home)\b[)\]]?\s*$", re.IGNORECASE)
_QUALIFIER = re.compile(r"\s*[\[(][^\])]{0,30}[\])]\s*$")
_SUB_BULLETS = "◦○∙·‣⁃▸►-*"
_ORG_WORDS = frozenset(
    "enterprise enterprises solutions systems technologies technology labs lab group services industries "
    "global digital software networks partners capital bank university institute foundation corporation "
    "company inc ltd llc pvt limited studio studios consulting ventures holdings media health energy motors "
    "airlines communications entertainment analytics robotics security cloud data ai apps".split()
)
_CONNECTORS = frozenset("of and for the at in de la & to on a an".split())
_LINK_WORDS = re.compile(r"\b(?:link|links|github|http|https|www|tech stack|repository|repo)\b", re.I)
_VERBS = frozenset(
    "led built developed designed implemented managed created worked improved reduced increased "
    "delivered launched wrote maintained owned drove migrated automated deployed optimized analyzed "
    "collaborated mentored architected integrated refactored established responsible handled "
    "contributed participated assisted supported coordinated conducted performed achieved ran "
    "spearheaded initiated resolved researched tested documented configured monitored scaled "
    "streamlined enhanced executed produced presented trained taught perform build develop design implement "
    "manage create work improve reduce increase deliver launch write maintain own drive migrate automate "
    "deploy optimize analyze collaborate mentor architect integrate refactor establish handle contribute "
    "participate assist support coordinate conduct achieve run lead spearhead initiate resolve research "
    "test document configure monitor scale streamline enhance execute produce present train teach use "
    "utilize leverage apply ensure provide help serve review plan define collect process prepare "
    "organize organise track measure coach responsible".split()
)
_PLACE = re.compile(
    r"^(?:remote|hybrid|on-?site|work from home|wfh)$|^[A-Z][A-Za-z.'’ -]+,\s*[A-Za-z .'’-]{2,}$", re.I
)
_PROJECT_TECH = re.compile(r"\s+\|\s+")


@dataclass(frozen=True)
class Anchor:
    line: int
    head: str
    head_offset: int  # offset of ``head`` within the line (after any bullet)
    tail: str
    range_span: tuple  # (start, end) within the line
    start: date | None
    end: date | None
    is_current: bool
    precision: str
    dates_ok: bool
    section: str | None
    fallback: bool


@dataclass(frozen=True)
class Frag:
    source: str  # "H" header, "P" previous line, "N" next line, "T" tail
    kind: str
    text: str
    line: int
    offset: int  # within the line
    low: bool = False  # relied on a flagged location guess


@dataclass(frozen=True)
class EntryProposal:
    kind: str
    title: str
    organization: str
    start: date | None
    end: date | None
    is_current: bool
    precision: str
    skills: tuple
    spans: dict = field(compare=False)
    snippet: str = ""
    status: str = "resolved"  # resolved | title_only | ambiguous | project
    layout: str = ""
    needs_check: bool = False
    layout_inferred: bool = False
    rule_fallback: bool = False
    default_accept: bool = False
    extractor: str = "rule"

    def as_data(self):
        """The dict :func:`resume_facts.clean_entry` accepts."""
        return {
            "kind": self.kind,
            "title": self.title,
            "organization": self.organization,
            "start": self.start.strftime("%Y-%m") if self.start else None,
            "end": self.end.strftime("%Y-%m") if self.end else None,
            "is_current": self.is_current,
            "precision": self.precision,
            "skills": list(self.skills),
        }


# --------------------------------------------------------------------------
# Dates (EX1-EX4)
# --------------------------------------------------------------------------
def _month_number(name):
    return _MONTHS.get(name[:3].lower())


def parse_date(token, *, end=False):
    """``(date(y, m, 1), precision)`` or ``None``. Year-only: January for a
    start, December for an end (EX2). Two-digit years need an apostrophe."""
    token = token.strip()
    match = re.fullmatch(rf"(\d{{1,2}})\s+({_MON})\s+(\d{{4}})", token, re.I)
    if match:
        month = _month_number(match.group(2))
        return (date(int(match.group(3)), month, 1), "month") if month else None
    match = re.fullmatch(rf"({_MON})\s*,?\s*(\d{{4}}|['’]\d{{2}})", token, re.I)
    if match:
        month, year = _month_number(match.group(1)), match.group(2)
        if year[0] in "'’":
            year = int(year[1:])
            year += 2000 if year <= 30 else 1900
        return (date(int(year), month, 1), "month") if month else None
    match = re.fullmatch(r"(\d{1,2})/(\d{4})", token)
    if match:
        month = int(match.group(1))
        return (date(int(match.group(2)), month, 1), "month") if 1 <= month <= 12 else None
    match = re.fullmatch(r"(\d{4})-(\d{2})", token)
    if match:
        month = int(match.group(2))
        return (date(int(match.group(1)), month, 1), "month") if 1 <= month <= 12 else None
    match = re.fullmatch(r"(\d{4})", token)
    if match:
        return (date(int(match.group(1)), 12 if end else 1, 1), "year")
    return None


def parse_range(text, today=None):
    """``(start, end, is_current, precision, plausible)`` for a date-range text,
    or ``None`` if it does not parse."""
    match = _RANGE.fullmatch(text.strip())
    if not match:
        return None
    first = parse_date(match.group(1))
    if first is None:
        return None
    current = bool(_CURRENT.match(match.group(2).strip()))
    last = None if current else parse_date(match.group(2), end=True)
    if not current and last is None:
        return None
    precision = "year" if first[1] == "year" or (last and last[1] == "year") else "month"
    start, end = first[0], (last[0] if last else None)
    return start, end, current, precision, dates_plausible(start, end, current, today)


# --------------------------------------------------------------------------
# Fragment classification (EX7)
# --------------------------------------------------------------------------
def _is_region_only(text):
    """True when the text is exactly a region or country (``Andhra Pradesh``,
    ``India``), judged by the location engine. City-only matches are ignored:
    the engine resolves bare company names to cities ("Google" -> Topeka)."""
    from apps.locations.engine import normalize_location

    tokens = text.split()
    if not 1 <= len(tokens) <= 3 or not all(t[:1].isupper() for t in tokens):
        return False
    result = normalize_location(text)
    return bool(result and result.get("resolved") and not result.get("city")
                and (result.get("region") or result.get("country")))


def classify(text):
    """TITLE / ORG / LOC / DETAIL for a short piece of header text."""
    piece = text.strip(" •●▪■-–—|,;:")
    if not piece or len(piece) > 90 or len(piece.split()) > MAX_HEAD_TOKENS or piece.endswith("."):
        return DETAIL
    if not any(ch.isalpha() for ch in piece):
        return DETAIL
    words = piece.split()
    if words[0].casefold().strip(".,:") in _VERBS:
        return DETAIL
    lowercase = [w for w in words if w[:1].islower() and w.casefold() not in _CONNECTORS]
    if len(lowercase) >= 2:
        return DETAIL  # a sentence fragment, not a name
    parts = [p.strip() for p in piece.split(",")]
    if piece.count(",") >= 2 or (len(parts) > 1 and any(lex.canonical_skill(p) for p in parts)):
        return DETAIL  # a technology list
    if _PLACE.match(piece) or _is_region_only(piece):
        return LOC
    if has_title_word(piece):
        return TITLE
    return ORG


def _peel_location(text):
    """Remove a location glued to the end of an employer/title.

    Returns ``(text, low_confidence)``. ``Remote``, a resolved ``City, Region``
    and a multi-word region are removed with confidence. A bare trailing city
    or country is also removed (``BrowserStack Mumbai``) but flagged, because the
    location engine resolves some ordinary words to cities; an entry that relied
    on such a guess never becomes a default. Only suffixes are removed, so the
    remainder is still a contiguous span."""
    from apps.accounts.regions import country_choices
    from apps.locations.engine import normalize_location

    text = _REMOTE_TAIL.sub("", text).rstrip(" ,-–—|")
    if "," in text:
        before, _, after = text.rpartition(",")
        head_words, after_words = before.split(), after.split()
        if 1 <= len(after_words) <= 3:
            for k in (3, 2, 1):
                if len(head_words) > k and all(w[:1].isupper() for w in head_words[-k:]):
                    candidate = " ".join(head_words[-k:]) + ", " + " ".join(after_words)
                    found = normalize_location(candidate)
                    if found and found.get("resolved") and found.get("city") and (
                        found.get("region") or found.get("country")
                    ):
                        return " ".join(head_words[:-k]).rstrip(" ,-–—|"), False
    words = text.split()
    for k in (3, 2):
        if len(words) > k and all(w[:1].isupper() for w in words[-k:]):
            tail = " ".join(words[-k:])
            if _is_region_only(tail):
                return " ".join(words[:-k]).rstrip(" ,-–—|"), False
    # A country name, or a single city, glued on without a comma: flagged guess.
    countries = {label.casefold() for _, label in country_choices()}
    for k in (4, 3, 2, 1):
        if len(words) > k:
            tail, rest = " ".join(words[-k:]), words[:-k]
            if rest[-1].casefold() in _CONNECTORS:
                continue
            if tail.casefold() in countries and tail[:1].isupper():
                return " ".join(rest).rstrip(" ,-–—|"), True
    for k in (2, 1):
        if len(words) > k:
            tail, rest = words[-k:], words[:-k]
            if not all(w[:1].isupper() for w in tail) or tail[-1].casefold() in _ORG_WORDS:
                continue
            if lex.canonical_skill(" ".join(tail)) or rest[-1].casefold() in _CONNECTORS:
                continue
            found = normalize_location(" ".join(tail))
            if found and found.get("resolved") and found.get("city") and (found.get("region") or found.get("country")):
                return " ".join(rest).rstrip(" ,-–—|"), True
    return text, False


def _is_sub_bullet(line):
    stripped = line.lstrip()
    return bool(stripped) and stripped[0] in _SUB_BULLETS and (
        stripped[0] != "-" and stripped[0] != "*" or stripped[1:2] == " "
    )


def _split_head(head):
    """Split anchor-line header text on separators; returns [(text, offset)]."""
    pieces, position = [], 0
    for sep in _SEPARATORS.finditer(head):
        pieces.append((head[position:sep.start()], position))
        position = sep.end()
    pieces.append((head[position:], position))
    if len(pieces) == 1 and head.count(",") == 1:
        left, right = head.split(",", 1)
        pieces = [(left, 0), (right, len(left) + 1)]
    out = []
    for text, offset in pieces:
        stripped = text.strip()
        if stripped:
            out.append((stripped, offset + (len(text) - len(text.lstrip()))))
    return out


def _clean_fragment(text, offset):
    """Strip a glued location/``Remote`` and a trailing qualifier ("(Contract)")
    from an organization/title text; returns ``(text, offset, low_confidence)``."""
    peeled, low = _peel_location(text.strip())
    cleaned = _QUALIFIER.sub("", peeled).strip(" ,-–—|")
    cleaned = _QUALIFIER.sub("", cleaned).strip(" ,-–—|")
    return cleaned, offset, low


# --------------------------------------------------------------------------
# Anchors (EX1, EX13)
# --------------------------------------------------------------------------
def _find_anchors(doc, today):
    sections = doc.sections
    has_experience = any(s.kind == sec.EXPERIENCE for s in sections)
    anchors = []
    for index, line in enumerate(doc.lines):
        if not line or index in doc.noise or len(line) > MAX_LINE:
            continue
        section = sec.section_of(sections, index)
        kind = section.kind if section else None
        if kind in (sec.EXPERIENCE, sec.PROJECTS):
            fallback = False
        elif not has_experience and kind in (None, sec.SUMMARY):
            fallback = True
        else:
            continue  # Education, Skills, Certs, Other: never entries (EX13)
        match = _RANGE.search(line)
        if not match:
            continue
        head_raw = line[: match.start()]
        glyph = bullet_of(head_raw.strip()) if head_raw.strip() else None
        head = strip_bullet(head_raw) if glyph else head_raw.strip()
        head_offset = line.find(head) if head else match.start()
        if _DEGREE.search(head) or len(head.split()) > MAX_HEAD_TOKENS or head.endswith("."):
            continue
        parsed = parse_range(match.group(0), today)
        if parsed is None:
            continue
        start, end, current, precision, plausible = parsed
        anchors.append(
            Anchor(
                line=index, head=head, head_offset=max(head_offset, 0), tail=line[match.end():].strip(" -–—|,"),
                range_span=match.span(), start=start if plausible else None, end=end if plausible else None,
                is_current=current and plausible, precision=precision, dates_ok=plausible,
                section=kind, fallback=fallback,
            )
        )
    return anchors


# --------------------------------------------------------------------------
# Assignment (EX7-EX9)
# --------------------------------------------------------------------------
def _neighbour_lines(doc, anchors_by_line, index, step, count):
    """Up to ``count`` non-empty, non-noise lines above (step=-1) or below (+1)."""
    found, position = [], index + step
    while 0 <= position < len(doc.lines) and len(found) < count and abs(position - index) <= 4:
        line = doc.lines[position]
        if position in doc.noise or (line and sec.heading_kind(line)):
            break
        if line:
            if position in anchors_by_line:
                break
            found.append(position)
        position += step
    return found


def _frags_for(doc, anchor, anchors_by_line):
    frags = []
    for text, offset in _split_head(anchor.head):
        cleaned, offset, low = _clean_fragment(text, offset)
        if cleaned:
            frags.append(Frag("H", classify(cleaned), cleaned, anchor.line, anchor.head_offset + offset, low))
    for position in _neighbour_lines(doc, anchors_by_line, anchor.line, -1, LOOKAROUND):
        line = doc.lines[position]
        if _is_sub_bullet(line):
            continue
        stripped = strip_bullet(line)
        base = line.find(stripped)
        for text, offset in _split_head(stripped):
            cleaned, _, low = _clean_fragment(text, 0)
            if cleaned:
                frags.append(Frag("P", classify(cleaned), cleaned, position, base + offset, low))
    for position in _neighbour_lines(doc, anchors_by_line, anchor.line, +1, 1):
        line = doc.lines[position]
        if _is_sub_bullet(line):
            continue
        stripped = strip_bullet(line)
        base = line.find(stripped)
        for text, offset in _split_head(stripped):
            cleaned, _, low = _clean_fragment(text, 0)
            if cleaned:
                frags.append(Frag("N", classify(cleaned), cleaned, position, base + offset, low))
    if anchor.tail:
        for text, offset in _split_head(anchor.tail):
            if classify(text) == LOC:
                frags.append(Frag("T", LOC, text, anchor.line, anchor.range_span[1] + offset))
    return frags


def _pick(frags):
    """``(status, title_frag, org_frag, layout)`` per rule EX7."""
    titles = [f for f in frags if f.kind == TITLE and f.source != "T"]
    orgs = [f for f in frags if f.kind == ORG]
    if len(titles) == 1:
        title = titles[0]
        different = [f for f in orgs if f.source != title.source]
        org = (different or orgs or [None])[0]
        layout = {"H": "title_in_header", "P": "title_in_previous", "N": "title_in_next"}[title.source]
        return ("resolved" if org else "title_only"), title, org, layout
    return "ambiguous", None, None, ""


def _layout_vote(resolved_layouts):
    counts = Counter(resolved_layouts)
    if not counts:
        return None
    layout, votes = counts.most_common(1)[0]
    return layout if votes >= 2 else None


def _apply_layout(frags, layout):
    """Resolve an ambiguous anchor with the document's majority layout."""
    source = {"title_in_header": "H", "title_in_previous": "P", "title_in_next": "N"}[layout]
    usable = [f for f in frags if f.kind in (TITLE, ORG) and f.source != "T"]
    here = [f for f in usable if f.source == source]
    if not here:
        return None
    title = here[0]
    others = [f for f in usable if f is not title and f.kind == ORG]
    different = [f for f in others if f.source != source]
    return title, (different or others or [None])[0]


# --------------------------------------------------------------------------
# Skills (EA1-EA5)
# --------------------------------------------------------------------------
_BASE_PATTERN = None


def _boundary(spelling):
    return rf"(?<![\w+#.-]){re.escape(spelling)}(?![\w+#]|\.\w)"


def _skill_pattern(declared):
    """One alternation of every lexicon spelling and declared skill, longest first."""
    global _BASE_PATTERN
    spellings = {spelling for spelling, _ in lex.all_spellings()}
    spellings |= {d.raw.casefold() for d in declared}
    ordered = sorted(spellings, key=lambda s: (-len(s), s))
    if not declared and _BASE_PATTERN is not None:
        return _BASE_PATTERN
    pattern = re.compile("|".join(_boundary(s) for s in ordered), re.IGNORECASE)
    if not declared:
        _BASE_PATTERN = pattern
    return pattern


def _attach_skills(doc, first_line, last_line, declared, pattern):
    """Skills mentioned inside lines [first_line, last_line) -> [(name, start, end)]."""
    declared_raw = {d.raw for d in declared}
    declared_names = {d.name.casefold() for d in declared}
    found, seen = [], set()
    for index in range(first_line, last_line):
        line = doc.lines[index]
        if not line or index in doc.noise:
            continue
        matches = list(pattern.finditer(line))
        plain = sum(1 for m in matches if not lex.is_ambiguous(lex.canonical_skill(m.group()) or m.group()))
        for match in matches:
            raw = match.group()
            name = lex.canonical_skill(raw) or next((d.name for d in declared if d.raw.casefold() == raw.casefold()), raw)
            if lex.is_ambiguous(name) or lex.is_ambiguous(raw):
                exact = raw == name or raw in declared_raw
                listed = name.casefold() in declared_names or plain >= 2
                if not (exact and listed):  # EA3: "go to market", "express interest"
                    continue
            if name.casefold() in seen:
                continue
            seen.add(name.casefold())
            found.append((name, doc.offsets[index] + match.start(), doc.offsets[index] + match.end()))
            if len(found) >= MAX_ENTRY_SKILLS:
                return found
    return found


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------
def _span(doc, frag):
    start = doc.offsets[frag.line] + frag.offset
    return (start, start + len(frag.text))


def _quality_ok(title, organization, kind):
    """Defaults are only for tidy entries (EX16): short names, no link text,
    brackets, colons or digit-only tokens."""
    for value, limit in ((title, 6), (organization, 6)):
        if not value:
            continue
        if len(value.split()) > limit or _LINK_WORDS.search(value) or re.search(r"[\[\]:|]", value):
            return False
        if any(token.isdigit() for token in value.split()):
            return False
        if re.search(r"\b[A-Z] [a-z]{3,}\b", value):  # unrepaired kerning artifact
            return False
    return True


def _build_entry(doc, anchor, kind, status, title, org, layout, block, declared, pattern, *, inferred=False):
    first, last = block
    skills = _attach_skills(doc, anchor.line, last, declared, pattern)
    title_text = title.text if title else anchor.head or ""
    spans = {
        "title": _span(doc, title) if title else (doc.offsets[anchor.line] + anchor.head_offset,
                                                  doc.offsets[anchor.line] + anchor.head_offset + len(anchor.head)),
        "organization": _span(doc, org) if org else None,
        "dates": (doc.offsets[anchor.line] + anchor.range_span[0], doc.offsets[anchor.line] + anchor.range_span[1]),
        "block": (doc.offsets[anchor.line], doc.offsets[last - 1] + len(doc.lines[last - 1])),
        "extent": (doc.offsets[first], doc.offsets[last - 1] + len(doc.lines[last - 1])),
        "skills": tuple((s, e) for _, s, e in skills),
    }
    complete = bool(title) and (kind == "project" or bool(org))
    clean = _quality_ok(title_text, org.text if org else "", kind)
    guessed = bool((title and title.low) or (org and org.low))  # relied on a flagged location guess
    needs_check = (
        status in ("ambiguous", "title_only") or not anchor.dates_ok or not title or not clean or guessed
    )
    accept = (
        complete and not needs_check and not anchor.fallback and anchor.precision == "month"
        and (anchor.start is not None and (anchor.end is not None or anchor.is_current)
             if kind == "experience" else bool(skills))
    )
    return EntryProposal(
        kind=kind, title=title_text, organization=org.text if org else "", start=anchor.start, end=anchor.end,
        is_current=anchor.is_current, precision=anchor.precision, skills=tuple(n for n, _, _ in skills),
        spans=spans, snippet=make_snippet(doc.text, *spans["dates"]), status=status, layout=layout,
        needs_check=needs_check, layout_inferred=inferred, rule_fallback=anchor.fallback, default_accept=accept,
    )


def _natural(entry):
    month = entry.start.strftime("%Y-%m") if entry.start else "nodate"
    return (entry.kind, re.sub(r"\s+", " ", f"{entry.organization.casefold()}|{entry.title.casefold()}|{month}"))


def _merge_duplicates(entries):
    """EX11: same natural key within one document -> one entry. Skills are
    united together with their spans, and the block/extent widen to cover both
    occurrences so every skill stays grounded inside the block (G2)."""
    merged = {}
    for entry in entries:
        key = _natural(entry)
        kept = merged.get(key)
        if kept is None:
            merged[key] = entry
            continue
        known = {name.casefold() for name in kept.skills}
        names, spans = list(kept.skills), list(kept.spans["skills"])
        for name, span in zip(entry.skills, entry.spans["skills"]):
            if name.casefold() not in known and len(names) < MAX_ENTRY_SKILLS:
                names.append(name)
                spans.append(span)
                known.add(name.casefold())
        widened = {
            "block": (min(kept.spans["block"][0], entry.spans["block"][0]), max(kept.spans["block"][1], entry.spans["block"][1])),
            "extent": (min(kept.spans["extent"][0], entry.spans["extent"][0]), max(kept.spans["extent"][1], entry.spans["extent"][1])),
            "skills": tuple(spans),
        }
        merged[key] = dataclasses.replace(kept, skills=tuple(names), spans={**kept.spans, **widened})
    return list(merged.values())


def _undated_projects(doc, declared, pattern, taken_lines):
    """EX15: a short title line in the PROJECTS section followed by bullets."""
    out = []
    for index in sec.lines_of(doc.sections, sec.PROJECTS):
        line = doc.lines[index]
        if index in taken_lines or index in doc.noise or not line or len(line) > 100 or bullet_of(line):
            continue
        head = _PROJECT_TECH.split(line, maxsplit=1)[0].strip()
        if (
            not head or head.endswith(".") or len(head.split()) > 10 or _RANGE.search(line)
            or classify(head) == DETAIL
        ):
            continue
        following = next((j for j in range(index + 1, min(index + 3, len(doc.lines))) if doc.lines[j]), None)
        if following is None or not bullet_of(doc.lines[following]):
            continue
        end = following
        while end < len(doc.lines) and end - index < MAX_BLOCK_LINES and (
            not doc.lines[end] or bullet_of(doc.lines[end]) or end == following
        ):
            if doc.lines[end] and not bullet_of(doc.lines[end]):
                break
            end += 1
        end = max(end, following + 1)
        skills = _attach_skills(doc, index, end, declared, pattern)
        if not skills:
            continue  # EX15: a project needs a grounded skill or a date range
        title_frag = Frag("H", ORG, head, index, 0)
        spans = {
            "title": _span(doc, title_frag), "organization": None, "dates": None,
            "block": (doc.offsets[index], doc.offsets[end - 1] + len(doc.lines[end - 1])),
            "extent": (doc.offsets[index], doc.offsets[end - 1] + len(doc.lines[end - 1])),
            "skills": tuple((s, e) for _, s, e in skills),
        }
        out.append(EntryProposal(
            kind="project", title=head, organization="", start=None, end=None, is_current=False,
            precision="month", skills=tuple(n for n, _, _ in skills), spans=spans,
            snippet=make_snippet(doc.text, *spans["title"]), status="project", layout="project",
            default_accept=True,
        ))
    return out


def extract_entries(normalized, today=None):
    """All experience and project entry proposals for a document."""
    today = today or timezone.localdate()
    doc = rules.prepare(normalized)
    declared = rules.declared_skills(doc)
    pattern = _skill_pattern(declared)
    anchors = _find_anchors(doc, today)
    anchors_by_line = {a.line: a for a in anchors}

    # Pass 1: choose a title/employer for every anchor (EX7).
    picks = []
    for anchor in anchors:
        frags = _frags_for(doc, anchor, anchors_by_line)
        if anchor.section == sec.PROJECTS:
            title = next((f for f in frags if f.kind in (TITLE, ORG) and f.source in ("H", "P")), None)
            picks.append({"anchor": anchor, "kind": "project", "status": "project", "title": title,
                          "org": None, "layout": "project", "inferred": False})
            continue
        status, title, org, layout = _pick(frags)
        picks.append({"anchor": anchor, "kind": "experience", "status": status, "title": title,
                      "org": org, "layout": layout, "inferred": False, "frags": frags})
    vote = _layout_vote([p["layout"] for p in picks if p["status"] == "resolved"])
    for pick in picks:
        if pick["kind"] == "experience" and pick["status"] == "ambiguous" and vote:
            chosen = _apply_layout(pick["frags"], vote)
            if chosen:
                pick["title"], pick["org"] = chosen
                pick["status"] = "resolved" if pick["org"] else "title_only"
                pick["layout"], pick["inferred"] = vote, True

    # Pass 2: blocks. Lines above an anchor that the *next* entry selected as its
    # title/employer belong to that entry, not to this one (EX10).
    def own_first_line(pick):
        used = [f.line for f in (pick["title"], pick["org"]) if f and f.source == "P"]
        return min([pick["anchor"].line] + used)

    entries = []
    for position, pick in enumerate(picks):
        anchor = pick["anchor"]
        section = sec.section_of(doc.sections, anchor.line)
        limit = section.end if section else len(doc.lines)
        if position + 1 < len(picks):
            limit = min(limit, own_first_line(picks[position + 1]))
        end = max(min(limit, anchor.line + MAX_BLOCK_LINES), anchor.line + 1)
        entries.append(
            _build_entry(
                doc, anchor, pick["kind"], pick["status"], pick["title"], pick["org"], pick["layout"],
                (own_first_line(pick), end), declared, pattern, inferred=pick["inferred"],
            )
        )

    entries = _merge_duplicates(entries)
    entries.extend(_undated_projects(doc, declared, pattern, set(anchors_by_line)))
    experience = [e for e in entries if e.kind == "experience"][:MAX_EXPERIENCE]
    projects = [e for e in entries if e.kind == "project"][:MAX_PROJECTS]
    return [e for e in experience + projects if entry_grounded(doc.text, e)]


# --------------------------------------------------------------------------
# Grounding (G2)
# --------------------------------------------------------------------------
def _inside(span, outer):
    return span is not None and outer[0] <= span[0] < span[1] <= outer[1]


def entry_grounded(text, entry):
    """Re-derive the title, employer, dates and skills from their spans and check
    that each lies where it should (G1, G2)."""
    spans = entry.spans
    try:
        extent, block = spans["extent"], spans["block"]
        if not (0 <= extent[0] < extent[1] <= len(text)):
            return False
        title = spans["title"]
        if not _inside(title, extent) or clean_text(text[title[0]:title[1]], max_len=80, min_len=2) != entry.title:
            return False
        org = spans["organization"]
        if entry.organization:
            if not _inside(org, extent) or clean_text(text[org[0]:org[1]], max_len=100, min_len=2) != entry.organization:
                return False
        elif org is not None:
            return False
        dates = spans["dates"]
        if dates is not None:
            if not _inside(dates, block):
                return False
            parsed = parse_range(text[dates[0]:dates[1]])
            if parsed is None:
                return False
            start, end, current, precision, plausible = parsed
            if plausible and (start, end, current, precision) != (entry.start, entry.end, entry.is_current, entry.precision):
                return False
            if not plausible and (entry.start or entry.end or entry.is_current):
                return False
        elif entry.start or entry.end or entry.is_current:
            return False
        if len(spans["skills"]) != len(entry.skills):
            return False
        for name, span in zip(entry.skills, spans["skills"]):
            if not _inside(span, block):
                return False
            raw = text[span[0]:span[1]]
            if (lex.canonical_skill(raw) or raw) != name and raw.casefold() != name.casefold():
                return False
    except (ImportValueError, KeyError, TypeError):
        return False
    return True


# --------------------------------------------------------------------------
# Derived profile fields from entries (FE1, FT1)
# --------------------------------------------------------------------------
def derived_fields(doc_text, entries):
    """``current_employer`` and ``target_titles`` proposals from *resolved*
    experience entries (an ambiguous entry never feeds a profile field)."""
    from apps.accounts.importing.grounding import Proposal

    resolved = [e for e in entries if e.kind == "experience" and e.status == "resolved"
                and not e.layout_inferred and e.title and e.organization]
    proposals = []
    current = [e for e in resolved if e.is_current and e.start]
    if current:
        latest = max(e.start for e in current)
        top = [e for e in current if e.start == latest]
        if len(top) == 1:
            org = top[0]
            s, e = org.spans["organization"]
            proposals.append(Proposal("current_employer", org.organization, ((s, e),), make_snippet(doc_text, s, e)))
    ordered = sorted((e for e in resolved if e.start), key=lambda e: e.start, reverse=True)
    titles, spans = [], []
    for entry in ordered[:3]:
        if entry.title.casefold() not in {t.casefold() for t in titles}:
            titles.append(entry.title)
            spans.append(entry.spans["title"])
    if titles:
        proposals.append(Proposal("target_titles", titles, tuple(spans), make_snippet(doc_text, *spans[0])))
    return proposals


# --------------------------------------------------------------------------
# Probes for ``manage.py import_eval`` (counts only)
# --------------------------------------------------------------------------
def classes_probe(normalized):
    """Per-entry resolution classes for the document, summed across documents."""
    counts = Counter()
    for entry in extract_entries(normalized):
        if entry.kind == "project":
            counts["project"] += 1
        else:
            counts[entry.status + ("_inferred" if entry.layout_inferred else "")] += 1
        counts["default_accept"] += entry.default_accept
    return dict(counts)


def anchors_probe(normalized):
    doc = rules.prepare(normalized)
    anchors = _find_anchors(doc, timezone.localdate())
    return {
        "anchors": len(anchors),
        "dates_plausible": sum(1 for a in anchors if a.dates_ok),
        "education_leaks": sum(
            1 for a in anchors
            if (sec.section_of(doc.sections, a.line) or sec.Section("", 0, 0, 0, False)).kind == sec.EDUCATION
            or _DEGREE.search(a.head)
        ),
    }


PROBES = {"entry_classes": classes_probe, "anchor_dates": anchors_probe}
