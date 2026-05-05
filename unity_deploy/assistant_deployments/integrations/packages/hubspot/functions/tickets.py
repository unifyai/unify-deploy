"""HubSpot Tickets - on-demand CRUD + sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_hubspot_ticket(ticket_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot ticket by ID."""
    if mock:
        return {
            "id": str(ticket_id),
            "properties": {
                "subject": "Maintenance: leaky faucet in unit 3B",
                "content": "Resident reports a steady drip from the kitchen faucet.",
                "hs_pipeline": "0",
                "hs_pipeline_stage": "1",
                "hs_ticket_priority": "MEDIUM",
                "source_type": "EMAIL",
                "hubspot_owner_id": "60001",
                "createdate": "2026-04-25T09:30:00Z",
                "hs_lastmodifieddate": "2026-04-25T09:30:00Z",
                "hs_object_id": str(ticket_id),
            },
            "createdAt": "2026-04-25T09:30:00Z",
            "updatedAt": "2026-04-25T09:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    default_props = [
        "subject",
        "content",
        "hs_pipeline",
        "hs_pipeline_stage",
        "hs_ticket_priority",
        "source_type",
        "hubspot_owner_id",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        ",".join(cfg["ticket_properties"])
        if isinstance(cfg["ticket_properties"], list)
        else ",".join(default_props)
    )
    return await hubspot_get(
        f"/crm/v3/objects/tickets/{ticket_id}",
        params={"properties": properties, "archived": "false"},
    )


@custom_function()
async def search_hubspot_tickets(query: str, limit: int = 10, mock: bool = True) -> dict:
    """Search HubSpot tickets (matches subject + content)."""
    if mock:
        return {
            "results": [
                {
                    "id": "8001",
                    "properties": {
                        "subject": "Maintenance: leaky faucet in unit 3B",
                        "hs_pipeline_stage": "1",
                        "hs_ticket_priority": "MEDIUM",
                        "hs_object_id": "8001",
                    },
                    "createdAt": "2026-04-25T09:30:00Z",
                    "updatedAt": "2026-04-25T09:30:00Z",
                    "archived": False,
                },
            ],
            "total": 1,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    default_props = [
        "subject",
        "content",
        "hs_pipeline",
        "hs_pipeline_stage",
        "hs_ticket_priority",
        "source_type",
        "hubspot_owner_id",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    return await hubspot_search(
        "tickets",
        query=query,
        properties=default_props,
        limit=min(limit, 100),
    )


@custom_function()
async def list_hubspot_tickets(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot tickets."""
    if mock:
        base_props = {
            "subject": "Maintenance: leaky faucet in unit 3B",
            "content": "Resident reports a steady drip.",
            "hs_pipeline": "0",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "MEDIUM",
            "hubspot_owner_id": "60001",
            "createdate": "2026-04-25T09:30:00Z",
            "hs_lastmodifieddate": "2026-04-25T09:30:00Z",
        }
        return {
            "results": [
                {
                    "id": str(8000 + i),
                    "properties": {**base_props, "hs_object_id": str(8000 + i)},
                    "createdAt": "2026-04-25T09:30:00Z",
                    "updatedAt": "2026-04-25T09:30:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "subject",
        "content",
        "hs_pipeline",
        "hs_pipeline_stage",
        "hs_ticket_priority",
        "source_type",
        "hubspot_owner_id",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/tickets", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_ticket(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot ticket.  ``subject`` and ``hs_pipeline_stage`` recommended."""
    if mock:
        base_props = {
            "subject": "New Ticket",
            "hs_pipeline": "0",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "MEDIUM",
        }
        return {
            "id": "99001",
            "properties": {**base_props, "hs_object_id": "99001", **properties},
            "createdAt": "2026-04-25T09:30:00Z",
            "updatedAt": "2026-04-25T09:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/tickets", {"properties": properties})


@custom_function()
async def update_hubspot_ticket(ticket_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch HubSpot ticket properties (useful for stage transitions)."""
    if mock:
        base_props = {
            "subject": "Maintenance: leaky faucet in unit 3B",
            "hs_pipeline": "0",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "MEDIUM",
        }
        return {
            "id": str(ticket_id),
            "properties": {**base_props, "hs_object_id": str(ticket_id), **properties},
            "createdAt": "2026-04-25T09:30:00Z",
            "updatedAt": "2026-04-25T09:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/tickets/{ticket_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_hubspot_tickets(
    since: str | None = None,
    schema_version: str = "hubspot.crm.tickets.v1",
    mock: bool = True,
) -> dict:
    """Sync tickets modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_ticket,
        )

        base_props = {
            "subject": "Maintenance: leaky faucet in unit 3B",
            "content": "Resident reports a steady drip.",
            "hs_pipeline": "0",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "MEDIUM",
            "hubspot_owner_id": "60001",
            "createdate": "2026-04-25T09:30:00Z",
            "hs_lastmodifieddate": "2026-04-25T09:30:00Z",
        }
        rows = [
            normalize_ticket(
                {
                    "id": str(8000 + i),
                    "properties": {**base_props, "hs_object_id": str(8000 + i)},
                    "createdAt": "2026-04-25T09:30:00Z",
                    "updatedAt": "2026-04-25T09:30:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"tickets": rows},
            "metadata": {
                "object_type": "tickets",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_ticket,
    )

    default_props = [
        "subject",
        "content",
        "hs_pipeline",
        "hs_pipeline_stage",
        "hs_ticket_priority",
        "source_type",
        "hubspot_owner_id",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["ticket_properties"]
        if isinstance(cfg["ticket_properties"], list)
        else default_props
    )
    filter_groups = (
        [
            {
                "filters": [
                    {
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GT",
                        "value": since,
                    },
                ],
            },
        ]
        if since
        else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "tickets",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=properties,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"tickets": rows},
                "metadata": {
                    "object_type": "tickets",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_ticket(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"tickets": rows},
        "metadata": {
            "object_type": "tickets",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
