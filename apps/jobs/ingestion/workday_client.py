"""Workday client — thin sync wrapper around the vendored ``WorkdayScraper``.

Unlike Greenhouse/Lever/Ashby's simple per-company REST endpoints, Workday
has no stable public per-company board API: fetching a tenant requires
pagination-cap detection and facet-based subdivision (see
apps/jobs/ingestion/vendor/workday/scraper.py's module docstring). Rather
than reimplement that, this repo vendors jobhive's ``WorkdayScraper``
directly (not a ``jobhive-py`` pip dependency -- see
docs/plans/2026-07-21-001-feat-ats-platform-expansion-plan.md Key Technical
Decisions for why) and exposes the same ``fetch_jobs(board_token)``
interface every other client in this package exposes.

``board_token`` for Workday is the full careers URL (e.g.
``https://acme.wd3.myworkdayjobs.com/en-US/careers``), not a short slug like
the other three platforms -- see the plan's board_token convention note.
"""
import re

from .exceptions import WorkdayParseError, WorkdayUnavailable
from .normalizers import normalize_workday_job
from .vendor.workday.exceptions import ScraperError
from .vendor.workday.scraper import URL_PATTERN, WorkdayScraper

# URL_PATTERN's `company`/`instance`/`site` groups are permissive about which
# characters they accept ([^.]+ / [^/?#]+) -- a company segment containing a
# "/" still matches, e.g. "https://internal-host/evil.wd3.myworkdayjobs.com/
# site" parses as company="internal-host/evil". WorkdayScraper.fetch() then
# rebuilds the request URL via plain f-string interpolation
# (f"https://{company}.{instance}.myworkdayjobs.com/..."), and a "/" in
# `company` there splits the string's authority component early --
# confirmed with httpx.Request(...).url.host, the resulting request's real
# destination host becomes "internal-host", not myworkdayjobs.com at all.
# This is a live SSRF: discover_boards calls WorkdayClient.fetch_jobs on
# every new candidate token from an externally-maintained dataset, unattended
# and before any human review. Restricting company/site to a safe hostname/
# path-segment character set closes it -- reject before ever constructing a
# WorkdayScraper, so the vendored (unmodified) code never sees the crafted
# input.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WorkdayClient:
    # Bounds a single tenant's fetch (pagination + facet subdivision +
    # description enrichment can mean many sequential/concurrent requests).
    # Not None by default: nothing in dispatch.py or the callers in tasks.py
    # passes this explicitly, and discover_boards' Celery soft_time_limit=540
    # is shared across every platform in one run -- an unbounded Workday
    # fetch could otherwise consume the entire run's budget by itself.
    DEFAULT_MAX_FETCH_SECONDS = 120.0

    def __init__(self, *, timeout=30.0, max_fetch_seconds=DEFAULT_MAX_FETCH_SECONDS):
        self.timeout = timeout
        self.max_fetch_seconds = max_fetch_seconds

    def fetch_jobs(self, board_token):
        """Return a list of normalized job dicts for ``board_token``.

        Raises:
            WorkdayUnavailable: ``board_token`` isn't a recognizable Workday
                careers URL, the tenant/site wasn't found, or the fetch
                failed after the vendored scraper's own retries.
            WorkdayParseError: a response was reached but its body was
                malformed / unexpected shape.
        """
        # Reject anything that isn't a *.myworkdayjobs.com careers URL before
        # any request is made -- board_token here is user/dataset-supplied
        # and, unlike the other platforms' short slugs, is itself the fetch
        # destination, so this is the SSRF guard referenced in the plan's
        # Key Technical Decisions. WorkdayScraper.fetch() re-checks the same
        # pattern internally before it ever calls out (it never delegates
        # host construction to the raw input), so this pre-check exists to
        # fail fast with a clearly-typed error rather than as the only line
        # of defense.
        match = URL_PATTERN.match(board_token.rstrip("/"))
        if not match or not (
            _SAFE_LABEL_RE.match(match.group("company"))
            and _SAFE_LABEL_RE.match(match.group("site"))
        ):
            raise WorkdayParseError(
                f"board_token must be a Workday careers URL matching "
                f"https://{{company}}.{{instance}}.myworkdayjobs.com/{{site}} "
                f"(company/site limited to letters, digits, '_', '-') "
                f"— got {board_token!r}"
            )

        scraper = WorkdayScraper(
            board_token, timeout=self.timeout, max_fetch_seconds=self.max_fetch_seconds
        )
        try:
            # WorkdayScraper.fetch() already wraps its internal async/httpx
            # work in asyncio.run() and returns a plain list synchronously --
            # no additional bridging needed at this layer.
            jobs = scraper.fetch()
        except ScraperError as exc:
            raise WorkdayUnavailable(str(exc)) from exc
        except ValueError as exc:
            # Not raised explicitly by the vendored scraper today, but a
            # malformed JSON body from response.json() would surface as a
            # bare ValueError -- translate it the same way the other clients
            # translate a JSON parse failure.
            raise WorkdayParseError(str(exc)) from exc

        return [normalize_workday_job(job) for job in jobs]
