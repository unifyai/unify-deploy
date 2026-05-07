"""Matterport view statistics — engagement reads, time-bounded."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_matterport_view_stats(
    model_id: str,
    since: str | None = None,
    until: str | None = None,
    granularity: str = "day",
    mock: bool = True,
) -> dict:
    """Return aggregate engagement for a single model over a time window."""
    if mock:
        return {
            "model_id": str(model_id),
            "period": {"since": since, "until": until, "granularity": granularity},
            "total_views": 142,
            "unique_visitors": 96,
            "avg_seconds_on_tour": 184,
            "top_referrers": [
                {"referrer": "hubspot.acme.com/listing/4b", "count": 38},
                {"referrer": "zillow.com", "count": 22},
            ],
            "daily_breakdown": [
                {"day": "2026-04-28", "total_views": 22, "unique_visitors": 18},
                {"day": "2026-04-29", "total_views": 31, "unique_visitors": 25},
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    query = """
    query ViewStats($id: ID!, $since: String, $until: String, $granularity: String) {
      model(id: $id) {
        id
        stats(since: $since, until: $until, granularity: $granularity) {
          totalViews
          uniqueVisitors
          avgSeconds
          topReferrers { referrer count }
          breakdown { day totalViews uniqueVisitors avgSeconds }
        }
      }
    }
    """
    body = await matterport_graphql(
        query,
        variables={
            "id": model_id,
            "since": since,
            "until": until,
            "granularity": granularity,
        },
    )
    if isinstance(body, dict) and body.get("error"):
        return body
    stats = ((body.get("model") or {}).get("stats")) or {}
    return {
        "model_id": str(model_id),
        "period": {"since": since, "until": until, "granularity": granularity},
        "total_views": stats.get("totalViews") or 0,
        "unique_visitors": stats.get("uniqueVisitors") or 0,
        "avg_seconds_on_tour": stats.get("avgSeconds") or 0,
        "top_referrers": stats.get("topReferrers") or [],
        "daily_breakdown": stats.get("breakdown") or [],
    }


@custom_function()
async def list_matterport_view_events(
    model_id: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Granular per-session view events for a model.

    Used by the HubSpot lead-correlation join when ``utm_email`` instrumentation
    is in place on Showcase URLs.
    """
    if mock:
        return {
            "model_id": str(model_id),
            "events": [
                {
                    "event_id": "evt-mock-1",
                    "occurred_at": "2026-04-29T11:42:18Z",
                    "session_seconds": 312,
                    "referrer": "https://hubspot.acme.com/listing/4b",
                    "referrer_email": "alex@example.com",
                    "country": "US",
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    query = """
    query ViewEvents($id: ID!, $since: String, $until: String, $limit: Int) {
      model(id: $id) {
        viewEvents(since: $since, until: $until, limit: $limit) {
          id
          occurredAt
          sessionSeconds
          referrer
          utmEmail
          country
          userAgent
        }
      }
    }
    """
    body = await matterport_graphql(
        query,
        variables={"id": model_id, "since": since, "until": until, "limit": limit},
    )
    if isinstance(body, dict) and body.get("error"):
        return body
    items = ((body.get("model") or {}).get("viewEvents")) or []
    return {
        "model_id": str(model_id),
        "events": items,
    }


@custom_function()
async def sync_matterport_view_stats(
    since: str | None = None,
    schema_version: str = "matterport.view_stats.v1",
    mock: bool = True,
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

    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._config import (
        get_matterport_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
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
            "breakdown"
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
