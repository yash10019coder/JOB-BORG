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
from .exceptions import WorkdayParseError, WorkdayUnavailable
from .normalizers import normalize_workday_job
from .vendor.workday.exceptions import ScraperError
from .vendor.workday.scraper import URL_PATTERN, WorkdayScraper


class WorkdayClient:
    def __init__(self, *, timeout=30.0, max_fetch_seconds=None):
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
        if not URL_PATTERN.match(board_token.rstrip("/")):
            raise WorkdayParseError(
                f"board_token must be a Workday careers URL matching "
                f"https://{{company}}.{{instance}}.myworkdayjobs.com/{{site}} "
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
