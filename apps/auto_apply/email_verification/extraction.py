"""Pure, I/O-free verification email parsing, sender validation, and code extraction."""
from datetime import datetime, timezone
import email
from email.utils import parseaddr, parsedate_to_datetime
import re

CONTEXTUAL_PHRASING_PATTERNS = [
    r"\bverif(?:y|ication)\b",
    r"\bsecurity code\b",
    r"\bconfirmation code\b",
    r"\bgreenhouse\b",
    r"\benter\s+(?:the\s+)?code\b",
]

# Strict contextual regex looking specifically for verification/security code phrasing near 6 digits
CONTEXTUAL_CODE_REGEX = re.compile(
    r"(?:verification\s+code|security\s+code|confirmation\s+code|verification|verify)[^0-9\n]{0,25}\b(\d{6})\b",
    re.IGNORECASE,
)

BARE_SIX_DIGIT_REGEX = re.compile(r"\b(\d{6})\b")


def is_sender_allowed(sender_header: str, allowlist: list[str]) -> bool:
    """Check if the sender email/domain matches any allowed pattern in allowlist.

    Sender header can be 'Name <email@domain.com>' or 'email@domain.com'.
    Allowlist entries can be full domains ('greenhouse.io') or specific email addresses.
    """
    if not sender_header or not allowlist:
        return False

    _, email_addr = parseaddr(sender_header)
    email_addr = email_addr.lower().strip()
    if not email_addr or "@" not in email_addr:
        return False

    domain = email_addr.split("@", 1)[1]

    for allowed in allowlist:
        allowed = allowed.lower().strip()
        if not allowed:
            continue

        if "@" in allowed:
            # Full email comparison
            if email_addr == allowed:
                return True
        else:
            # Domain comparison
            allowed_domain = allowed[1:] if allowed.startswith("@") else allowed
            if domain == allowed_domain or domain.endswith("." + allowed_domain):
                return True

    return False


def strip_html_tags(html: str) -> str:
    """Strip HTML tags to yield plain text for extraction."""
    if not html:
        return ""
    text = re.sub(r"<(?:br|p|div|tr|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text


def has_contextual_phrasing(text: str) -> bool:
    """Check if text contains expected verification context phrasing."""
    if not text:
        return False
    return any(re.search(pat, text, re.IGNORECASE) for pat in CONTEXTUAL_PHRASING_PATTERNS)


def extract_code_from_text(subject: str, body: str) -> str | None:
    """Extract a 6-digit verification code from subject and body.

    Requires contextual verification phrasing to exist in subject+body.
    Prefers contextual code regex (code near verification phrasing).
    Returns code (str) or None.
    """
    combined_text = f"{subject or ''}\n{body or ''}"
    if not has_contextual_phrasing(combined_text):
        return None

    # First try contextual regex
    match = CONTEXTUAL_CODE_REGEX.search(combined_text)
    if match:
        return match.group(1)

    # Fallback: if contextual phrasing is present, find 6-digit numbers.
    six_digits = BARE_SIX_DIGIT_REGEX.findall(combined_text)
    unique_codes = set(six_digits)

    # If exactly one unique 6-digit code exists, return it
    if len(unique_codes) == 1:
        return six_digits[0]

    return None


def parse_email_message(msg_bytes: bytes) -> tuple[str, str, str, datetime | None]:
    """Parse raw RFC822 email bytes into (subject, from_header, body_text, msg_date)."""
    msg = email.message_from_bytes(msg_bytes)

    subject = str(msg.get("Subject", ""))
    from_header = str(msg.get("From", ""))

    date_header = msg.get("Date")
    msg_date: datetime | None = None
    if date_header:
        try:
            msg_date = parsedate_to_datetime(str(date_header))
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
        except Exception:
            msg_date = None

    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("latin-1", errors="replace")

            if content_type == "text/plain":
                body_text += "\n" + decoded
            elif content_type == "text/html":
                body_text += "\n" + strip_html_tags(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("latin-1", errors="replace")

            if msg.get_content_type() == "text/html":
                body_text = strip_html_tags(decoded)
            else:
                body_text = decoded

    return subject, from_header, body_text, msg_date


def evaluate_email_candidate(
    msg_bytes: bytes, since: datetime, sender_allowlist: list[str]
) -> str | None:
    """Evaluate a raw email message against filters and return extracted code or None."""
    subject, from_header, body_text, msg_date = parse_email_message(msg_bytes)

    if not is_sender_allowed(from_header, sender_allowlist):
        return None

    if since:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        if msg_date:
            if msg_date < since:
                return None

    return extract_code_from_text(subject, body_text)
