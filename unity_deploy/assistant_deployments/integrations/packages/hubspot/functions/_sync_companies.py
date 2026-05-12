"""HubSpot companies sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_companies(
    since: str | None = None,
    schema_version: str = "hubspot.crm.companies.v1",
    mock: bool = True,
) -> dict:
    """Sync companies modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_company,
        )

        base_props = {
            "name": "Acme Properties LLC",
            "domain": "acme-properties.com",
            "industry": "REAL_ESTATE",
            "city": "Tampa",
            "state": "FL",
            "lifecyclestage": "customer",
            "createdate": "2025-09-01T09:00:00Z",
            "hs_lastmodifieddate": "2026-04-20T11:00:00Z",
        }
        rows = [
            normalize_company(
                {
                    "id": str(5000 + i),
                    "properties": {**base_props, "hs_object_id": str(5000 + i)},
                    "createdAt": "2025-09-01T09:00:00Z",
                    "updatedAt": "2026-04-20T11:00:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"companies": rows},
            "metadata": {
                "object_type": "companies",
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
        normalize_company,
    )

    default_props = [
        "name",
        "domain",
        "industry",
        "phone",
        "city",
        "state",
        "country",
        "numberofemployees",
        "annualrevenue",
        "lifecyclestage",
        "type",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["company_properties"]
        if isinstance(cfg["company_properties"], list)
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
            "companies",
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
                "tables": {"companies": rows},
                "metadata": {
                    "object_type": "companies",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_company(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"companies": rows},
        "metadata": {
            "object_type": "companies",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
