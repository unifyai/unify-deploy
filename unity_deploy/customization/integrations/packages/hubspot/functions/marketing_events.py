"""HubSpot External Marketing Events + attendance tracking."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_EVENT = {
    "id": "evt-7001",
    "eventName": "Spring Property Tour Day",
    "eventOrganizer": "ClientZeta Properties",
    "eventDescription": "Open-house tour at Sunset Tower and three sister properties.",
    "eventUrl": "https://example.com/events/spring-tour",
    "eventType": "PHYSICAL",
    "startDateTime": "2026-05-10T14:00:00Z",
    "endDateTime": "2026-05-10T17:00:00Z",
    "createdAt": "2026-04-01T10:00:00Z",
    "updatedAt": "2026-04-15T11:00:00Z",
}


@custom_function()
async def list_marketing_events(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_EVENT, "id": f"evt-{7000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/marketing-events", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def create_marketing_event(properties: dict, mock: bool = True) -> dict:
    """Create an external marketing event."""
    if mock:
        return {**_MOCK_EVENT, "id": "evt-99001", **properties}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/marketing/v3/marketing-events", properties)


@custom_function()
async def update_marketing_event(event_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_EVENT, "id": str(event_id), **properties}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(f"/marketing/v3/marketing-events/{event_id}", properties)


@custom_function()
async def list_event_attendance(
    event_id: str,
    state: str = "registered",
    mock: bool = True,
) -> dict:
    """List attendance records for a marketing event.  ``state`` is one of
    ``registered``, ``attended``, ``cancelled``, ``no_show``."""
    if mock:
        return {
            "event_id": str(event_id), "state": state,
            "results": [{"contact_id": "12345", "state": state.upper()}],
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/marketing/v3/marketing-events/{event_id}/attendance/{state}/read",
    )


@custom_function()
async def sync_marketing_events(
    schema_version: str = "hubspot.marketing.events.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "event_id": f"evt-{7000 + i}",
            "name": _MOCK_EVENT["eventName"],
            "organizer": _MOCK_EVENT["eventOrganizer"],
            "type": _MOCK_EVENT["eventType"],
            "start_time": _MOCK_EVENT["startDateTime"],
            "end_time": _MOCK_EVENT["endDateTime"],
            "url": _MOCK_EVENT["eventUrl"],
            "created_at": _MOCK_EVENT["createdAt"],
            "updated_at": _MOCK_EVENT["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_events": rows},
            "metadata": {"object_type": "marketing_events", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
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
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"marketing_events": rows},
                    "metadata": {"object_type": "marketing_events", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "event_id": str(r.get("id", "")),
                "name": r.get("eventName", ""),
                "organizer": r.get("eventOrganizer", ""),
                "type": r.get("eventType", ""),
                "start_time": r.get("startDateTime", ""),
                "end_time": r.get("endDateTime", ""),
                "url": r.get("eventUrl", ""),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_events": rows},
        "metadata": {"object_type": "marketing_events", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
