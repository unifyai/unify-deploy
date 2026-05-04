"""HubSpot Engagements - Meetings (log + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "hs_meeting_title", "hs_meeting_body", "hs_meeting_location",
    "hs_meeting_outcome", "hs_meeting_start_time", "hs_meeting_end_time",
    "hs_internal_meeting_notes", "hubspot_owner_id",
    "hs_timestamp", "hs_object_id", "hs_lastmodifieddate",
]

_MOCK_MEETING = {
    "id": "M1001",
    "properties": {
        "hs_meeting_title": "Property Tour - Sunset Tower",
        "hs_meeting_body": "Showed Units 3B and 7A; client preferred 7A.",
        "hs_meeting_location": "Sunset Tower lobby",
        "hs_meeting_outcome": "COMPLETED",
        "hs_meeting_start_time": "2026-04-26T14:00:00Z",
        "hs_meeting_end_time": "2026-04-26T15:00:00Z",
        "hubspot_owner_id": "60001",
        "hs_timestamp": "2026-04-26T14:00:00Z",
        "hs_object_id": "M1001",
        "hs_lastmodifieddate": "2026-04-26T15:30:00Z",
    },
    "createdAt": "2026-04-26T15:30:00Z",
    "updatedAt": "2026-04-26T15:30:00Z",
    "archived": False,
}


@custom_function()
async def log_meeting(
    title: str,
    body: str,
    start_time_iso: str,
    end_time_iso: str,
    location: str = "",
    outcome: str = "COMPLETED",
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Log a meeting against CRM records."""
    if mock:
        return {**_MOCK_MEETING, "id": "M99001",
                "properties": {**_MOCK_MEETING["properties"],
                               "hs_meeting_title": title, "hs_meeting_body": body,
                               "hs_meeting_start_time": start_time_iso,
                               "hs_meeting_end_time": end_time_iso}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body_payload: dict = {
        "properties": {
            "hs_meeting_title": title,
            "hs_meeting_body": body,
            "hs_meeting_location": location,
            "hs_meeting_outcome": outcome,
            "hs_meeting_start_time": start_time_iso,
            "hs_meeting_end_time": end_time_iso,
            "hs_timestamp": start_time_iso,
        },
    }
    if associations:
        body_payload["associations"] = _build_associations(
            associations, {"contact": 200, "company": 188, "deal": 212, "ticket": 226},
        )
    return await hubspot_post("/crm/v3/objects/meetings", body_payload)


@custom_function()
async def list_meetings(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_MEETING, "id": f"M{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/meetings", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_meetings(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.meetings.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )
        rows = [normalize_engagement({**_MOCK_MEETING, "id": f"M{1000 + i}"},
                                     engagement_type="meeting") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"meetings": rows},
            "metadata": {"object_type": "meetings", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_engagement,
    )

    cfg = get_hubspot_config()
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "meetings",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"meetings": rows},
                    "metadata": {"object_type": "meetings", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_engagement(r, engagement_type="meeting")
                    for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"meetings": rows},
        "metadata": {"object_type": "meetings", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }


def _build_associations(refs: list[dict], type_ids: dict[str, int]) -> list[dict]:
    out = []
    for ref in refs:
        obj = ref.get("object_type", "").rstrip("s")
        type_id = type_ids.get(obj)
        if type_id is None:
            continue
        out.append({
            "to": {"id": str(ref["id"])},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": type_id}],
        })
    return out
