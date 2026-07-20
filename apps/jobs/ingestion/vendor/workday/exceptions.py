# Vendored (trimmed) from jobhive (github.com/kalil0321/ats-scrapers),
# commit a20e56dcae253a4a71871c280fc691fa1a3fba79, MIT licensed.
# Only the classes WorkdayScraper actually raises/imports are kept --
# ManifestError/StorageError are dataset-client-only and not needed here.
"""Exception hierarchy for the vendored Workday scraper."""


class JobHiveError(Exception):
    """Base class for all vendored jobhive errors."""


class ScraperError(JobHiveError):
    """Raised when the scraper fails to fetch or parse jobs."""


class CompanyNotFoundError(ScraperError):
    """Raised when a company is not present on Workday."""
