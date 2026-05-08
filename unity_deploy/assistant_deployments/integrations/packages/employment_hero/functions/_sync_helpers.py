"""Sync-orchestrator helpers for the Employment Hero package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Provides:

* ``load_sync_state`` / ``load_sync_state_rows`` / ``load_latest_run`` —
  read the persistent watermark + audit rows from DataManager.
* ``get_state_watermark`` — pluck a single ``last_synced_at`` from the
  state map.
* ``seconds_since`` — pure ISO-8601 delta.
"""

from __future__ import annotations


async def load_sync_state() -> dict:
    """Read the per-object watermark map from DataManager.  Returns an
    empty dict on the first run before any state row has been written.
    """
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncState",
            limit=200,
        )
    except Exception:
        return {}
    return {r["object_type"]: r for r in (rows or []) if r.get("object_type")}


async def load_sync_state_rows() -> list:
    """Same as ``load_sync_state`` but returns the raw row list (stable
    shape for the public ``get_employmenthero_sync_state``)."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        return await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncState",
            limit=200,
        )
    except Exception:
        return []


async def load_latest_run() -> dict | None:
    """Most recent ``sync_runs`` row, or ``None`` when no runs have been
    recorded yet."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncRuns",
            limit=1,
            order_by="started_at desc",
        )
    except Exception:
        return None
    return (rows or [None])[0]


def get_state_watermark(state: dict, object_key: str) -> str | None:
    """Pull the ``last_synced_at`` timestamp for an object type."""
    row = state.get(object_key) if state else None
    if not row:
        return None
    return row.get("last_synced_at")


def seconds_since(iso_ts: str | None) -> int | None:
    """Return seconds since an ISO-8601 timestamp; ``None`` if no input
    or unparseable input."""
    if not iso_ts:
        return None
    import datetime as _dt

    try:
        parsed = _dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    delta = _dt.datetime.now(tz=_dt.timezone.utc) - parsed
    return int(delta.total_seconds())
