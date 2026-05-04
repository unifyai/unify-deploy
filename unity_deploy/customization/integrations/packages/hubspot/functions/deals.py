"""HubSpot Deals - on-demand CRUD + stage transitions + sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_deal(deal_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot deal by ID."""
    if mock:
        return {
            "id": str(deal_id),
            "properties": {
                "dealname": "Acme Properties - Q3 Management Contract",
                "dealstage": "presentationscheduled", "pipeline": "default",
                "amount": "120000", "closedate": "2026-09-30T00:00:00Z",
                "createdate": "2026-04-10T08:00:00Z",
                "hs_lastmodifieddate": "2026-04-22T15:00:00Z",
                "hubspot_owner_id": "60001", "dealtype": "newbusiness",
                "description": "Annual property management contract for portfolio of 12 multifamily assets.",
                "hs_object_id": str(deal_id), "hs_priority": "high",
                "num_associated_contacts": "3",
            },
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-22T15:00:00Z",
            "archived": False,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    default_props = [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
        "dealtype", "description", "hs_object_id", "hs_priority",
        "num_associated_contacts",
    ]
    cfg = get_hubspot_config()
    properties = (
        ",".join(cfg["deal_properties"])
        if isinstance(cfg["deal_properties"], list)
        else ",".join(default_props)
    )
    return await hubspot_get(
        f"/crm/v3/objects/deals/{deal_id}",
        params={"properties": properties, "archived": "false"},
    )


@custom_function()
async def search_deals(query: str, limit: int = 10, mock: bool = True) -> dict:
    """Search HubSpot deals (matches dealname + description)."""
    if mock:
        return {
            "results": [{
                "id": "9001",
                "properties": {
                    "dealname": "Acme Properties - Q3 Management Contract",
                    "dealstage": "presentationscheduled", "pipeline": "default",
                    "amount": "120000", "hs_object_id": "9001",
                    "hubspot_owner_id": "60001",
                },
                "createdAt": "2026-04-10T08:00:00Z",
                "updatedAt": "2026-04-22T15:00:00Z",
                "archived": False,
            }],
            "total": 1,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    default_props = [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
        "dealtype", "description", "hs_object_id", "hs_priority",
        "num_associated_contacts",
    ]
    return await hubspot_search(
        "deals", query=query, properties=default_props, limit=min(limit, 100),
    )


@custom_function()
async def list_deals(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot deals."""
    if mock:
        base_props = {
            "dealname": "Acme Properties - Q3 Management Contract",
            "dealstage": "presentationscheduled", "pipeline": "default",
            "amount": "120000", "hubspot_owner_id": "60001",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-22T15:00:00Z",
        }
        return {
            "results": [
                {"id": str(9000 + i),
                 "properties": {**base_props, "hs_object_id": str(9000 + i)},
                 "createdAt": "2026-04-10T08:00:00Z",
                 "updatedAt": "2026-04-22T15:00:00Z",
                 "archived": False}
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
        "dealtype", "description", "hs_object_id", "hs_priority",
        "num_associated_contacts",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/deals", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_deal(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot deal.  At minimum supply ``dealname`` and ``pipeline``."""
    if mock:
        base_props = {
            "dealname": "New Deal", "dealstage": "appointmentscheduled",
            "pipeline": "default", "amount": "0",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
        }
        return {
            "id": "99001",
            "properties": {**base_props, "hs_object_id": "99001", **properties},
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-10T08:00:00Z",
            "archived": False,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/deals", {"properties": properties})


@custom_function()
async def update_deal(deal_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch HubSpot deal properties."""
    if mock:
        base_props = {
            "dealname": "Acme Properties - Q3 Management Contract",
            "dealstage": "presentationscheduled", "pipeline": "default",
            "amount": "120000",
        }
        return {
            "id": str(deal_id),
            "properties": {**base_props, "hs_object_id": str(deal_id), **properties},
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-22T15:00:00Z",
            "archived": False,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/deals/{deal_id}",
        {"properties": properties},
    )


@custom_function()
async def transition_deal_stage(
    deal_id: str,
    new_stage_id: str,
    note: str | None = None,
    mock: bool = True,
) -> dict:
    """Move a deal to a new pipeline stage.  Optionally records a note
    explaining the transition."""
    if mock:
        base_props = {
            "dealname": "Acme Properties - Q3 Management Contract",
            "pipeline": "default", "amount": "120000",
        }
        return {
            "id": str(deal_id),
            "properties": {**base_props, "dealstage": new_stage_id,
                           "hs_object_id": str(deal_id)},
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-22T15:00:00Z",
            "archived": False,
            "note_added": bool(note),
        }

    import datetime as _dt

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch, hubspot_post,
    )

    result = await hubspot_patch(
        f"/crm/v3/objects/deals/{deal_id}",
        {"properties": {"dealstage": new_stage_id}},
    )
    if note and "error" not in result:
        ts_ms = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000)
        await hubspot_post("/crm/v3/objects/notes", {
            "properties": {"hs_note_body": note, "hs_timestamp": str(ts_ms)},
            "associations": [{
                "to": {"id": str(deal_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 214}],  # note->deal
            }],
        })
    return result


@custom_function()
async def delete_deal(deal_id: str, mock: bool = True) -> dict:
    """Soft-delete a HubSpot deal.  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "id": str(deal_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {"error": "Deletes are disabled.  Set HUBSPOT_ALLOW_DELETE=true.",
                "id": str(deal_id)}
    return await hubspot_delete(f"/crm/v3/objects/deals/{deal_id}")


@custom_function()
async def sync_deals(
    since: str | None = None,
    schema_version: str = "hubspot.crm.deals.v1",
    mock: bool = True,
) -> dict:
    """Sync deals modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_deal,
        )
        base_props = {
            "dealname": "Acme Properties - Q3 Management Contract",
            "dealstage": "presentationscheduled", "pipeline": "default",
            "amount": "120000", "hubspot_owner_id": "60001",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-22T15:00:00Z",
        }
        rows = [
            normalize_deal({
                "id": str(9000 + i),
                "properties": {**base_props, "hs_object_id": str(9000 + i)},
                "createdAt": "2026-04-10T08:00:00Z",
                "updatedAt": "2026-04-22T15:00:00Z",
                "archived": False,
            })
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"deals": rows},
            "metadata": {"object_type": "deals", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_deal,
    )

    default_props = [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
        "dealtype", "description", "hs_object_id", "hs_priority",
        "num_associated_contacts",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["deal_properties"]
        if isinstance(cfg["deal_properties"], list)
        else default_props
    )
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "deals",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=properties,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"deals": rows},
                    "metadata": {"object_type": "deals", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_deal(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"deals": rows},
        "metadata": {"object_type": "deals", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }
