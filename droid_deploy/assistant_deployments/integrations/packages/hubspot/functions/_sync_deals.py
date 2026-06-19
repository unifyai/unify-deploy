"""HubSpot deals sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_deals(
    since: str | None = None,
    schema_version: str = "hubspot.crm.deals.v1",
    mock: bool = True,
) -> dict:
    """Sync deals modified since ``since`` into a tables envelope."""
    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_deal,
        )

        base_props = {
            "dealname": "Acme Properties - Q3 Management Contract",
            "dealstage": "presentationscheduled",
            "pipeline": "default",
            "amount": "120000",
            "hubspot_owner_id": "60001",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-22T15:00:00Z",
        }
        rows = [
            normalize_deal(
                {
                    "id": str(9000 + i),
                    "properties": {**base_props, "hs_object_id": str(9000 + i)},
                    "createdAt": "2026-04-10T08:00:00Z",
                    "updatedAt": "2026-04-22T15:00:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"deals": rows},
            "metadata": {
                "object_type": "deals",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_deal,
    )

    default_props = [
        "dealname",
        "dealstage",
        "pipeline",
        "amount",
        "closedate",
        "createdate",
        "hs_lastmodifieddate",
        "hubspot_owner_id",
        "dealtype",
        "description",
        "hs_object_id",
        "hs_priority",
        "num_associated_contacts",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["deal_properties"]
        if isinstance(cfg["deal_properties"], list)
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
            "deals",
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
                "tables": {"deals": rows},
                "metadata": {
                    "object_type": "deals",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_deal(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"deals": rows},
        "metadata": {
            "object_type": "deals",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
