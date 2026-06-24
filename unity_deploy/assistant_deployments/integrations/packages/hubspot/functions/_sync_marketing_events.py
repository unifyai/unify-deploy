"""HubSpot marketing_events sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_marketing_events(
    schema_version: str = "hubspot.marketing.events.v1",
    mock: bool = True,
) -> dict:
    """Sync marketing event definitions into a tables envelope."""
    if mock:
        rows = [
            {
                "event_id": f"evt-{7000 + i}",
                "name": "Spring Property Tour Day",
                "organizer": "ClientZeta Properties",
                "type": "PHYSICAL",
                "start_time": "2026-05-10T14:00:00Z",
                "end_time": "2026-05-10T17:00:00Z",
                "url": "https://example.com/events/spring-tour",
                "created_at": "2026-04-01T10:00:00Z",
                "updated_at": "2026-04-15T11:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_events": rows},
            "metadata": {
                "object_type": "marketing_events",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/marketing/v3/marketing-events", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_events": rows},
                "metadata": {
                    "object_type": "marketing_events",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "event_id": str(r.get("id", "")),
                    "name": r.get("eventName", ""),
                    "organizer": r.get("eventOrganizer", ""),
                    "type": r.get("eventType", ""),
                    "start_time": r.get("startDateTime", ""),
                    "end_time": r.get("endDateTime", ""),
                    "url": r.get("eventUrl", ""),
                    "created_at": r.get("createdAt", ""),
                    "updated_at": r.get("updatedAt", ""),
                },
            )
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_events": rows},
        "metadata": {
            "object_type": "marketing_events",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
