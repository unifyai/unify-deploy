"""Matterport view-stats sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_matterport_sync_tick`` orchestrator via importlib;
not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_matterport_view_stats(
    since: str | None = None,
    schema_version: str = "matterport.view_stats.v1",
    mock: bool = False,
) -> dict:
    """Sync per-model daily view stats into DataManager.

    Pulls the lookback window for every model present in
    ``Matterport/Models``.  When ``MATTERPORT_SYNC_VIEW_EVENTS`` is true,
    also emits granular per-session events for the lead-correlation join.
    """
    import datetime as _dt

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "view_stats_daily": [
                    {
                        "model_id": "mdl-mock-1",
                        "day": "2026-04-29",
                        "total_views": 31,
                        "unique_visitors": 25,
                        "avg_seconds": 198,
                        "top_referrer": "hubspot.acme.com/listing/4b",
                    },
                ],
            },
            "metadata": {
                "since": since,
                "fetched_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
                "mode": "mock",
            },
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._config import (
        get_matterport_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        normalize_view_stats_day,
        normalize_view_event,
    )

    cfg = get_matterport_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    until = _dt.datetime.now(tz=_dt.timezone.utc)
    lookback_since = (
        until - _dt.timedelta(days=cfg["view_stats_lookback_days"])
    ).isoformat()
    period_since = since or lookback_since
    period_until = until.isoformat()

    dm = ManagerRegistry.get_data_manager()
    try:
        models = await dm.filter("Matterport/Models", limit=2000) or []
    except Exception:
        models = []

    daily_rows: list[dict] = []
    event_rows: list[dict] = []
    errors: list[dict] = []
    sync_events = cfg["sync_view_events"]

    stats_query = """
    query Stats($id: ID!, $since: String, $until: String) {
      model(id: $id) {
        stats(since: $since, until: $until, granularity: "day") {
          breakdown { day totalViews uniqueVisitors avgSeconds topReferrer }
        }
      }
    }
    """
    events_query = """
    query Events($id: ID!, $since: String, $until: String, $limit: Int) {
      model(id: $id) {
        viewEvents(since: $since, until: $until, limit: $limit) {
          id occurredAt sessionSeconds referrer utmEmail country userAgent
        }
      }
    }
    """

    for m in models:
        model_id = m.get("model_id")
        if not model_id:
            continue
        body = await matterport_graphql(
            stats_query,
            variables={"id": model_id, "since": period_since, "until": period_until},
        )
        if isinstance(body, dict) and body.get("error"):
            errors.append({"model_id": model_id, "error": body.get("error")})
            continue
        breakdown = ((body.get("model") or {}).get("stats") or {}).get(
            "breakdown",
        ) or []
        for raw in breakdown:
            daily_rows.append(normalize_view_stats_day(raw, model_id))

        if sync_events:
            ev_body = await matterport_graphql(
                events_query,
                variables={
                    "id": model_id,
                    "since": period_since,
                    "until": period_until,
                    "limit": 500,
                },
            )
            if isinstance(ev_body, dict) and not ev_body.get("error"):
                events = ((ev_body.get("model") or {}).get("viewEvents")) or []
                for raw in events:
                    event_rows.append(normalize_view_event(raw, model_id))

    tables: dict[str, list] = {"view_stats_daily": daily_rows}
    if sync_events:
        tables["view_events"] = event_rows
    return {
        "schema_version": schema_version,
        "tables": tables,
        "metadata": {
            "since": period_since,
            "until": period_until,
            "fetched_at": started,
            "model_count": len(models),
            "row_count": len(daily_rows),
            "event_count": len(event_rows),
            "errors": errors,
            "mode": "live",
        },
    }
