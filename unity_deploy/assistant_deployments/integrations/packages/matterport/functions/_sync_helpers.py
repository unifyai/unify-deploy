"""Sync-orchestrator helpers for the Matterport package.

Underscore-prefixed so FunctionManager skips this file at discovery.
"""

from __future__ import annotations


async def load_sync_state() -> dict:
    """Read the per-object watermark map from DataManager.  Returns an
    empty dict on the first run before any state row has been written.
    """
    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter("Matterport/Meta/SyncState", limit=200)
    except Exception:
        return {}
    return {r["object_type"]: r for r in (rows or []) if r.get("object_type")}


async def load_sync_state_rows() -> list:
    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        return await dm.filter("Matterport/Meta/SyncState", limit=200)
    except Exception:
        return []


async def load_latest_run() -> dict | None:
    from unify.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "Matterport/Meta/SyncRuns",
            limit=1,
            order_by="started_at desc",
        )
    except Exception:
        return None
    return (rows or [None])[0]


def get_state_watermark(state: dict, object_key: str) -> str | None:
    row = state.get(object_key) if state else None
    if not row:
        return None
    return row.get("last_synced_at")


def seconds_since(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    import datetime as _dt

    try:
        parsed = _dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    delta = _dt.datetime.now(tz=_dt.timezone.utc) - parsed
    return int(delta.total_seconds())


# GraphQL fragment listing the fields fetched for every ``Model`` query.
# Lives here (not at the top of ``models.py``) because each
# ``@custom_function()`` body runs under FunctionManager isolation that
# strips module-level names — so the f-strings referencing this fragment
# must import it from inside the body, the same pattern ``normalize_model``
# below already uses.
MODEL_FIELDS_FRAGMENT = """
  id
  name
  internalLabel
  visibility
  shareUrl
  status
  modifiedAt
  sqft
  address {
    addressLine1
    addressLine2
    city
    state
    postalCode
    country
  }
"""


def normalize_model(raw: dict) -> dict:
    """Flatten a Matterport ``Model`` GraphQL node to a DataManager row.

    Defensive about missing fields — Matterport's schema evolves.  Address
    is denormalised so simple equality joins on city/postal_code work.
    """
    address = raw.get("address") or {}
    return {
        "model_id": raw.get("id"),
        "name": raw.get("name"),
        "internal_label": raw.get("internalLabel"),
        "visibility": raw.get("visibility"),
        "share_url": raw.get("shareUrl") or raw.get("share_url"),
        "address_line1": address.get("addressLine1") or address.get("line1"),
        "address_line2": address.get("addressLine2") or address.get("line2"),
        "city": address.get("city"),
        "state": address.get("state") or address.get("region"),
        "postal_code": address.get("postalCode") or address.get("postal_code"),
        "country": address.get("country"),
        "sqft": raw.get("sqft") or raw.get("squareFeet"),
        "status": raw.get("status"),
        "last_modified": raw.get("modifiedAt") or raw.get("modified_at"),
    }


def normalize_view_stats_day(raw: dict, model_id: str) -> dict:
    return {
        "model_id": model_id,
        "day": raw.get("day") or raw.get("date"),
        "total_views": raw.get("totalViews") or raw.get("views") or 0,
        "unique_visitors": raw.get("uniqueVisitors") or raw.get("uniques") or 0,
        "avg_seconds": raw.get("avgSeconds") or raw.get("avgTimeOnTour") or 0,
        "top_referrer": raw.get("topReferrer") or raw.get("referrer"),
    }


def normalize_view_event(raw: dict, model_id: str) -> dict:
    return {
        "event_id": raw.get("id") or raw.get("eventId"),
        "model_id": model_id,
        "occurred_at": raw.get("occurredAt") or raw.get("timestamp"),
        "session_seconds": raw.get("sessionSeconds") or raw.get("duration"),
        "referrer": raw.get("referrer"),
        "referrer_email": raw.get("utmEmail") or raw.get("utm_email"),
        "country": raw.get("country"),
        "user_agent": (raw.get("userAgent") or "")[:200],
    }
