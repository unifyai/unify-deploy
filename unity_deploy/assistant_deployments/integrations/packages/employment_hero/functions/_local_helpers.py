"""Local-query helpers for the Employment Hero package.

Underscore-prefixed so :func:`droid.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  Imported
from inside ``query_local_employmenthero_*`` function bodies so
FunctionManager's isolation rule is preserved.

Provides:

* ``safe_filter`` — wraps ``dm.filter`` with empty-list-on-error semantics.
* ``freshness`` — returns ``{last_synced_at, is_fresh, threshold_seconds}``
  for an object type by reading the sync state row + the per-object
  cadence config.
"""

from __future__ import annotations


async def safe_filter(
    dm,
    context: str,
    *,
    filter: str | None = None,
    limit: int = 100,
) -> list:
    """Wrapper around ``dm.filter`` that returns ``[]`` on error so the
    caller doesn't have to wrap every analytical query in its own
    try/except.  Mirrors HubSpot's ``safe_filter`` convention.
    """
    try:
        return await dm.filter(context, filter=filter, limit=limit) or []
    except Exception:
        return []


async def freshness(object_type: str) -> dict:
    """Return ``{last_synced_at, is_fresh, threshold_seconds}`` for an
    object.  Threshold is the configured per-object cadence × 2 unless
    ``EMPLOYMENTHERO_LOCAL_FRESHNESS_THRESHOLD_SECONDS`` is set.
    """
    from droid.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )

    dm = ManagerRegistry.get_data_manager()
    cfg = get_employmenthero_config()

    try:
        rows = await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncState",
            filter=f"`object_type` == '{object_type}'",
            limit=1,
        )
    except Exception:
        rows = []
    last = rows[0]["last_synced_at"] if rows else None

    override = cfg["local_freshness_threshold_seconds"]
    threshold = (
        override
        if override is not None
        else (
            cfg["object_intervals"].get(object_type, cfg["sync_min_interval_seconds"])
            * 2
        )
    )

    if not last:
        return {
            "last_synced_at": None,
            "is_fresh": False,
            "threshold_seconds": threshold,
            "hint": "No sync state row — has the sync orchestrator run?",
        }

    import datetime as _dt

    try:
        parsed = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
        age = (_dt.datetime.now(tz=_dt.timezone.utc) - parsed).total_seconds()
    except (ValueError, TypeError):
        return {
            "last_synced_at": last,
            "is_fresh": False,
            "threshold_seconds": threshold,
            "hint": "Unparseable last_synced_at.",
        }
    return {
        "last_synced_at": last,
        "is_fresh": age <= threshold,
        "threshold_seconds": threshold,
        "age_seconds": int(age),
    }
