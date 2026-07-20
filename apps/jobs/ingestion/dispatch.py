"""ATS-keyed client dispatch.

Central mapping from ``JobSource.ATS`` value to client class, replacing
per-call-site ``GreenhouseClient()`` instantiation. Adding a new ATS is a
one-line addition here plus a registered client class -- no other call site
(``ingest_source``, ``discover_boards``, ``register_job_source``, the
``add_job_source`` command) needs to change.
"""
from apps.jobs.models import JobSource

from .ashby_client import AshbyClient
from .greenhouse_client import GreenhouseClient
from .lever_client import LeverClient
from .workday_client import WorkdayClient

CLIENT_REGISTRY = {
    JobSource.ATS.GREENHOUSE: GreenhouseClient,
    JobSource.ATS.LEVER: LeverClient,
    JobSource.ATS.ASHBY: AshbyClient,
    JobSource.ATS.WORKDAY: WorkdayClient,
}


def get_client(ats, **kwargs):
    """Return a new client instance for ``ats``.

    Raises:
        ValueError: ``ats`` is not a registered ATS.
    """
    try:
        client_cls = CLIENT_REGISTRY[ats]
    except KeyError:
        raise ValueError(
            f"No ingestion client registered for ats={ats!r}. "
            f"Registered: {sorted(CLIENT_REGISTRY)}"
        ) from None
    return client_cls(**kwargs)
