"""HubSpot Engagements - Calls (log + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "hs_call_title", "hs_call_body", "hs_call_direction", "hs_call_duration",
    "hs_call_from_number", "hs_call_to_number", "hs_call_status",
    "hs_call_disposition", "hs_timestamp", "hubspot_owner_id",
    "hs_object_id", "hs_lastmodifieddate",
]

_MOCK_CALL = {
    "id": "C1001",
    "properties": {
        "hs_call_title": "Discovery call - prospective tenant",
        "hs_call_body": "Discussed move-in date, unit preferences, budget.",
        "hs_call_direction": "OUTBOUND",
        "hs_call_duration": "1200",
        "hs_call_from_number": "+1 555 200 0100",
        "hs_call_to_number": "+1 555 123 4567",
        "hs_call_status": "COMPLETED",
        "hs_call_disposition": "73a0d17f-1163-4015-bdd5-ec830791da20",
        "hs_timestamp": "2026-04-25T14:00:00Z",
        "hubspot_owner_id": "60001",
        "hs_object_id": "C1001",
        "hs_lastmodifieddate": "2026-04-25T14:30:00Z",
    },
    "createdAt": "2026-04-25T14:30:00Z",
    "updatedAt": "2026-04-25T14:30:00Z",
    "archived": False,
}


@custom_function()
async def log_call(
    title: str,
    body: str,
    direction: str = "OUTBOUND",
    duration_seconds: int = 0,
    from_number: str = "",
    to_number: str = "",
    status: str = "COMPLETED",
    timestamp_iso: str | None = None,
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Log a call against CRM records.

    ``associations`` is a list of ``{object_type, id}`` dicts pointing to
    contacts, companies, deals, or tickets the call relates to.
    """
    if mock:
        return {**_MOCK_CALL, "id": "C99001",
                "properties": {**_MOCK_CALL["properties"],
                               "hs_call_title": title, "hs_call_body": body,
                               "hs_call_direction": direction}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body_payload: dict = {
        "properties": {
            "hs_call_title": title,
            "hs_call_body": body,
            "hs_call_direction": direction,
            "hs_call_duration": str(duration_seconds),
            "hs_call_from_number": from_number,
            "hs_call_to_number": to_number,
            "hs_call_status": status,
            "hs_timestamp": timestamp_iso or _now_ms_str(),
        },
    }
    if associations:
        body_payload["associations"] = _build_associations(associations, _CALL_TYPE_IDS)
    return await hubspot_post("/crm/v3/objects/calls", body_payload)


@custom_function()
async def list_calls(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_CALL, "id": f"C{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/calls", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_calls(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.calls.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )
        rows = [normalize_engagement({**_MOCK_CALL, "id": f"C{1000 + i}"},
                                     engagement_type="call") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"calls": rows},
            "metadata": {"object_type": "calls", "mode": "mock",
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
            "calls",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"calls": rows},
                    "metadata": {"object_type": "calls", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_engagement(r, engagement_type="call")
                    for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"calls": rows},
        "metadata": {"object_type": "calls", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }


# HubSpot v4 association type IDs for note/call/etc -> primary CRM objects.
_CALL_TYPE_IDS = {"contact": 194, "company": 182, "deal": 206, "ticket": 220}


def _build_associations(refs: list[dict], type_ids: dict[str, int]) -> list[dict]:
    """Convert ``[{object_type, id}, ...]`` to HubSpot v4 association payload."""
    out = []
    for ref in refs:
        obj = ref.get("object_type", "").rstrip("s")  # "contacts" -> "contact"
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
