from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from common.settings import SETTINGS
from communication.infra.assistant_sessions import emit_observability_event

logger = logging.getLogger(__name__)

IDLE_JOB_POOL_REPLENISH_TIMEOUT_SECONDS = 30.0
IDLE_JOB_POOL_REPLENISH_MIN_INTERVAL_SECONDS = 5.0

_REPLENISH_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="idle-job-pool",
)
_REPLENISH_LOCK = threading.Lock()
_replenish_inflight = False
_replenish_last_started_at = 0.0


def request_idle_job_pool_replenishment(
    *,
    extra_demand: int = 0,
    source: str,
) -> dict[str, Any]:
    """Ask adapters to replenish idle Droid jobs for product demand.

    Args:
        extra_demand: Number of blocked `PendingJob` sessions that should be
            satisfied immediately in addition to the steady-state warm pool.
        source: Short caller identifier for logs and observability.

    Returns:
        The JSON response returned by adapters' `/scheduled/jobs/create`
        utility endpoint.

    Raises:
        RuntimeError: If the adapters endpoint responds with a non-200 status.
        requests.RequestException: If the request itself fails.
    """

    normalized_extra_demand = max(0, int(extra_demand))
    response = requests.post(
        f"{SETTINGS.adapters_url}/scheduled/jobs/create",
        params={"extra_demand": normalized_extra_demand},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=IDLE_JOB_POOL_REPLENISH_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Idle job pool replenish failed with "
            f"{response.status_code}: {response.text[:500]}",
        )
    result = response.json()
    emit_observability_event(
        "infra.idle_job_pool.replenish.completed",
        source=source,
        extra_demand=normalized_extra_demand,
        result=result,
    )
    return result


def _run_scheduled_replenishment(*, extra_demand: int, source: str) -> None:
    """Execute one scheduled replenish request and release the scheduler lock."""

    global _replenish_inflight
    try:
        request_idle_job_pool_replenishment(
            extra_demand=extra_demand,
            source=source,
        )
    except Exception as exc:  # pragma: no cover - safety net for background thread
        logger.exception("Idle job pool replenish failed")
        emit_observability_event(
            "infra.idle_job_pool.replenish.failed",
            source=source,
            extra_demand=max(0, int(extra_demand)),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        with _REPLENISH_LOCK:
            _replenish_inflight = False


def schedule_idle_job_pool_replenishment(
    *,
    extra_demand: int = 0,
    source: str,
    min_interval_seconds: float = IDLE_JOB_POOL_REPLENISH_MIN_INTERVAL_SECONDS,
) -> bool:
    """Schedule a background idle-job replenish request if one is not active.

    The scheduler collapses bursts of identical demand into a single background
    request so repeated controller reconciliations do not spam the adapters
    maintenance endpoint.
    """

    global _replenish_inflight, _replenish_last_started_at

    normalized_extra_demand = max(0, int(extra_demand))
    now = time.monotonic()
    with _REPLENISH_LOCK:
        if _replenish_inflight:
            emit_observability_event(
                "infra.idle_job_pool.replenish.skipped",
                source=source,
                extra_demand=normalized_extra_demand,
                reason="inflight",
            )
            return False
        if now - _replenish_last_started_at < min_interval_seconds:
            emit_observability_event(
                "infra.idle_job_pool.replenish.skipped",
                source=source,
                extra_demand=normalized_extra_demand,
                reason="rate_limited",
                min_interval_seconds=min_interval_seconds,
            )
            return False
        _replenish_inflight = True
        _replenish_last_started_at = now

    _REPLENISH_EXECUTOR.submit(
        _run_scheduled_replenishment,
        extra_demand=normalized_extra_demand,
        source=source,
    )
    emit_observability_event(
        "infra.idle_job_pool.replenish.requested",
        source=source,
        extra_demand=normalized_extra_demand,
    )
    return True
