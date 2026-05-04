"""HubSpot Engagements - Notes (create + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "hs_note_body", "hs_timestamp", "hubspot_owner_id",
    "hs_object_id", "hs_lastmodifieddate",
]

_MOCK_NOTE = {
    "id": "N1001",
    "properties": {
        "hs_note_body": "Owner prefers monthly statements via email; quarterly board updates.",
        "hs_timestamp": "2026-04-22T16:00:00Z",
        "hubspot_owner_id": "60001",
        "hs_object_id": "N1001",
        "hs_lastmodifieddate": "2026-04-22T16:00:00Z",
    },
    "createdAt": "2026-04-22T16:00:00Z",
    "updatedAt": "2026-04-22T16:00:00Z",
    "archived": False,
}


@custom_function()
async def create_note(
    body: str,
    timestamp_iso: str | None = None,
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Create a note attached to one or more CRM records."""
    if mock:
        return {**_MOCK_NOTE, "id": "N99001",
                "properties": {**_MOCK_NOTE["properties"], "hs_note_body": body}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body_payload: dict = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": timestamp_iso or _now_ms_str(),
        },
    }
    if associations:
        body_payload["associations"] = _build_associations(
            associations, {"contact": 202, "company": 190, "deal": 214, "ticket": 228},
        )
    return await hubspot_post("/crm/v3/objects/notes", body_payload)


@custom_function()
async def list_notes(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_NOTE, "id": f"N{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/notes", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_notes(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.notes.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )
        rows = [normalize_engagement({**_MOCK_NOTE, "id": f"N{1000 + i}"},
                                     engagement_type="note") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"notes": rows},
            "metadata": {"object_type": "notes", "mode": "mock",
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
            "notes",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"notes": rows},
                    "metadata": {"object_type": "notes", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_engagement(r, engagement_type="note")
                    for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"notes": rows},
        "metadata": {"object_type": "notes", "mode": "real",
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


def _now_ms_str() -> str:
    import datetime as _dt
    return str(int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000))
