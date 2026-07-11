"""HubSpot tickets sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_tickets(
    since: str | None = None,
    schema_version: str = "hubspot.crm.tickets.v1",
    mock: bool = True,
) -> dict:
    """Sync tickets modified since ``since`` into a tables envelope."""
    if mock:
        from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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
