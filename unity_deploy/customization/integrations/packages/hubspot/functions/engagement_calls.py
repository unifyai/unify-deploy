"""HubSpot Engagements - Calls (log + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


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
        return {
            "id": "C99001",
            "properties": {
                "hs_call_title": title, "hs_call_body": body,
                "hs_call_direction": direction,
                "hs_call_duration": str(duration_seconds),
                "hs_call_from_number": from_number,
                "hs_call_to_number": to_number,
                "hs_call_status": status,
                "hs_timestamp": timestamp_iso or "2026-04-25T14:00:00Z",
                "hubspot_owner_id": "60001",
                "hs_object_id": "C99001",
                "hs_lastmodifieddate": "2026-04-25T14:30:00Z",
            },
            "createdAt": "2026-04-25T14:30:00Z",
            "updatedAt": "2026-04-25T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._engagement_helpers import (
        build_associations, now_ms_str,
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
            "hs_timestamp": timestamp_iso or now_ms_str(),
        },
    }
    if associations:
        # HubSpot v4 association type ids for call -> primary CRM objects.
        type_ids = {"contact": 194, "company": 182, "deal": 206, "ticket": 220}
        body_payload["associations"] = build_associations(associations, type_ids)
    return await hubspot_post("/crm/v3/objects/calls", body_payload)


@custom_function()
async def list_calls(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    """Paginate through HubSpot call engagements."""
    if mock:
        base_props = {
            "hs_call_title": "Discovery call - prospective tenant",
            "hs_call_body": "Discussed move-in date, unit preferences, budget.",
            "hs_call_direction": "OUTBOUND",
            "hs_call_duration": "1200",
            "hs_call_status": "COMPLETED",
            "hs_timestamp": "2026-04-25T14:00:00Z",
            "hubspot_owner_id": "60001",
        }
        return {
            "results": [
                {"id": f"C{1000 + i}",
                 "properties": {**base_props, "hs_object_id": f"C{1000 + i}"},
                 "createdAt": "2026-04-25T14:30:00Z",
                 "updatedAt": "2026-04-25T14:30:00Z",
                 "archived": False}
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "hs_call_title", "hs_call_body", "hs_call_direction", "hs_call_duration",
        "hs_call_from_number", "hs_call_to_number", "hs_call_status",
        "hs_call_disposition", "hs_timestamp", "hubspot_owner_id",
        "hs_object_id", "hs_lastmodifieddate",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
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
    """Sync call engagements modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )
        base_props = {
            "hs_call_title": "Discovery call - prospective tenant",
            "hs_call_body": "Discussed move-in date, unit preferences, budget.",
            "hs_call_direction": "OUTBOUND", "hs_call_duration": "1200",
            "hs_call_status": "COMPLETED",
            "hs_timestamp": "2026-04-25T14:00:00Z",
            "hubspot_owner_id": "60001",
        }
        rows = [
            normalize_engagement(
                {"id": f"C{1000 + i}",
                 "properties": {**base_props, "hs_object_id": f"C{1000 + i}"},
                 "createdAt": "2026-04-25T14:30:00Z",
                 "updatedAt": "2026-04-25T14:30:00Z",
                 "archived": False},
                engagement_type="call",
            )
            for i in range(3)
        ]
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

    default_props = [
        "hs_call_title", "hs_call_body", "hs_call_direction", "hs_call_duration",
        "hs_call_from_number", "hs_call_to_number", "hs_call_status",
        "hs_call_disposition", "hs_timestamp", "hubspot_owner_id",
        "hs_object_id", "hs_lastmodifieddate",
    ]
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
            properties=default_props,
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
