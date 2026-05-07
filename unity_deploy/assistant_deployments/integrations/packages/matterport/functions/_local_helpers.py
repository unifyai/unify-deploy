"""Local DataManager query helpers for Matterport.

Underscore-prefixed so FunctionManager skips this file at discovery.
"""

from __future__ import annotations


async def safe_filter(dm, context: str, *, filter: str | None = None, limit: int = 100):
    try:
        return await dm.filter(context, filter=filter, limit=limit) or []
    except Exception:
        return []


async def freshness(object_key: str) -> dict:
    """Return ``{last_synced_at, is_fresh, threshold_seconds}`` for an object."""
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._config import (
        get_matterport_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        load_sync_state,
        seconds_since,
    )

    cfg = get_matterport_config()
    state = await load_sync_state()
    last_synced_at = state.get(object_key, {}).get("last_synced_at")

    threshold = cfg.get("local_freshness_threshold_seconds")
    if threshold is None:
        per_object = cfg["object_intervals"].get(
            object_key,
            cfg["sync_min_interval_seconds"],
        )
        threshold = per_object * 2

    age = seconds_since(last_synced_at)
    is_fresh = age is not None and age <= threshold
    return {
        "last_synced_at": last_synced_at,
        "age_seconds": age,
        "threshold_seconds": threshold,
        "is_fresh": bool(is_fresh),
    }
