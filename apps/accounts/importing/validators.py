"""Value validation shared by extraction and by the apply step (rules V1-V4, FU).

Everything here is pure: no database, no network. A value that fails raises
:class:`ImportValueError` carrying a short ``code`` (never the value itself,
which may be personal data), and the importer drops it instead of guessing.
"""
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from apps.accounts.regions import alpha3_country_map

# Field limits mirror the Profile model columns (V1).
MAX_TEXT = {
    "full_name": 255,
    "headline": 255,
    "current_employer": 255,
    "location_city": 255,
}
MAX_URL = 255
MAX_PHONE = 32
MAX_TITLE = 80
MAX_LIST = 50

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏  ⁠﻿]")
_TAG = re.compile(r"^[a-z0-9_+#.-]{1,40}$")
_USERNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_LINKEDIN_PATH = re.compile(r"^/in/([A-Za-z0-9_%-]{3,100})/?$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PHONE_CHARS = re.compile(r"^\+?\(?[0-9][0-9 ()./-]*$")
_DATE_LIKE = re.compile(r"^\d{4}\s*[-/.–]\s*\d{4}$")
_BAD_SUFFIXES = (".local", ".localhost", ".internal", ".test", ".invalid", ".example")
GITHUB_RESERVED = frozenset(
    "orgs settings topics features about pricing login join explore marketplace "
    "sponsors notifications new".split()
)
_URL_VALIDATOR = URLValidator(schemes=["https"])


class ImportValueError(ValueError):
    """A proposed value is not acceptable. ``code`` is safe to log and show."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def clean_text(raw, *, max_len=255, min_len=1):
    """V1: NFKC, trimmed, no control characters or newlines, no angle brackets."""
    if not isinstance(raw, str):
        raise ImportValueError("not_text")
    value = unicodedata.normalize("NFKC", raw).replace("\n", " ").replace("\r", " ")
    value = re.sub(r"\s+", " ", _CONTROL.sub("", value)).strip()
    if "<" in value or ">" in value:
        raise ImportValueError("markup")
    if not (min_len <= len(value) <= max_len):
        raise ImportValueError("length")
    if not any(ch.isalpha() for ch in value):
        raise ImportValueError("no_letters")
    return value


def clean_phone(raw):
    """FP2/FP3: 7-15 digits, a leading ``+`` kept, never a date range."""
    if not isinstance(raw, str):
        raise ImportValueError("not_text")
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
    if not value or len(value) > MAX_PHONE or not _PHONE_CHARS.match(value):
        raise ImportValueError("bad_phone")
    if _DATE_LIKE.match(value):
        raise ImportValueError("bad_phone")
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        raise ImportValueError("bad_phone")
    return value


def _host_ok(host):
    if not host or len(host) > 253 or not host.isascii():
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP literals are never a profile link
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_BAD_SUFFIXES):
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(label.startswith("xn--") for label in labels):
        return False
    if not all(_HOST_LABEL.match(label) for label in labels):
        return False
    return bool(re.fullmatch(r"[a-z]{2,24}", labels[-1]))


def clean_url(raw, *, kind=None):
    """FU1-FU6: an https link with a plain ASCII host; LinkedIn and GitHub links
    are reduced to their canonical profile form. Returns the canonical URL."""
    if not isinstance(raw, str):
        raise ImportValueError("not_text")
    value = unicodedata.normalize("NFKC", raw).strip().rstrip(".,;)")
    if not value or re.search(r"\s", value) or _CONTROL.search(value):
        raise ImportValueError("bad_url")
    if "://" in value:
        if not value.lower().startswith("https://"):
            raise ImportValueError("bad_scheme")
    elif re.match(r"^(javascript|data|file|ftp|mailto|tel|vbscript):", value, re.I):
        raise ImportValueError("bad_scheme")
    else:
        value = "https://" + value  # FU4: a scheme-less link is assumed https
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ImportValueError("bad_url") from None
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        raise ImportValueError("bad_url")
    host = (parts.hostname or "").lower()
    if not _host_ok(host):
        raise ImportValueError("bad_host")
    path = parts.path or ""
    if kind == "linkedin":
        if host not in ("linkedin.com", "www.linkedin.com") and not re.fullmatch(
            r"[a-z]{2,3}\.linkedin\.com", host
        ):
            raise ImportValueError("not_linkedin")
        match = _LINKEDIN_PATH.match(path)
        if not match:
            raise ImportValueError("not_linkedin_profile")
        canonical = f"https://www.linkedin.com/in/{match.group(1)}"
    elif kind == "github":
        if host not in ("github.com", "www.github.com"):
            raise ImportValueError("not_github")
        match = re.fullmatch(r"/([^/]+)/?", path)
        if (
            not match
            or not _USERNAME.match(match.group(1))
            or match.group(1).lower() in GITHUB_RESERVED
        ):
            raise ImportValueError("not_github_profile")
        canonical = f"https://github.com/{match.group(1)}"
    else:
        canonical = f"https://{host}{path}"
        if parts.query:
            canonical += f"?{parts.query}"
        elif path == "/":
            canonical = canonical.rstrip("/")
    if len(canonical) > MAX_URL:
        raise ImportValueError("length")
    try:
        _URL_VALIDATOR(canonical)
    except ValidationError:
        raise ImportValueError("bad_url") from None
    return canonical


def clean_country(raw):
    """V2: an ISO alpha-3 code from the known region list."""
    value = raw.strip().upper() if isinstance(raw, str) else ""
    if value not in alpha3_country_map():
        raise ImportValueError("bad_country")
    return value


def clean_tags(raw):
    """V2: at most 50 short, lowercase tag tokens; duplicates dropped."""
    if not isinstance(raw, (list, tuple)):
        raise ImportValueError("not_list")
    out = []
    for item in raw:
        tag = unicodedata.normalize("NFKC", str(item)).strip().lower()
        if not _TAG.match(tag):
            raise ImportValueError("bad_tag")
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_LIST:
        raise ImportValueError("too_many")
    return out


def clean_titles(raw):
    """FT1/V2: at most 50 job titles of 3-80 characters; duplicates dropped."""
    if not isinstance(raw, (list, tuple)):
        raise ImportValueError("not_list")
    out = []
    for item in raw:
        title = clean_text(str(item), max_len=MAX_TITLE, min_len=3)
        if title.endswith("."):
            raise ImportValueError("sentence")
        if title.casefold() not in {t.casefold() for t in out}:
            out.append(title)
    if len(out) > MAX_LIST:
        raise ImportValueError("too_many")
    return out


def clean_field(field, raw):
    """Validate a value for one importable Profile field; returns the cleaned value."""
    if field in MAX_TEXT:
        return clean_text(raw, max_len=MAX_TEXT[field])
    if field == "phone":
        return clean_phone(raw)
    if field == "linkedin_url":
        return clean_url(raw, kind="linkedin")
    if field == "github_url":
        return clean_url(raw, kind="github")
    if field == "portfolio_url":
        return clean_url(raw)
    if field == "location_country":
        return clean_country(raw)
    if field == "target_tags":
        return clean_tags(raw)
    if field == "target_titles":
        return clean_titles(raw)
    raise ImportValueError("not_importable")
