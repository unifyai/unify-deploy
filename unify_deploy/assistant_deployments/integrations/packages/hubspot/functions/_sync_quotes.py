"""HubSpot quotes sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_quotes(
    since: str | None = None,
    schema_version: str = "hubspot.crm.quotes.v1",
    mock: bool = True,
) -> dict:
    """Sync quotes modified since ``since`` into a tables envelope."""
    if mock:
        from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_quote,
        )

        base_props = {
            "hs_title": "Acme Properties - Q3 2026 Management Quote",
            "hs_status": "DRAFT",
            "hs_expiration_date": "2026-07-31",
            "hubspot_owner_id": "60001",
            "createdate": "2026-04-22T10:00:00Z",
            "hs_lastmodifieddate": "2026-04-22T10:00:00Z",
        }
        rows = [
            normalize_quote(
                {
                    "id": str(6000 + i),
                    "properties": {**base_props, "hs_object_id": str(6000 + i)},
                    "createdAt": "2026-04-22T10:00:00Z",
                    "updatedAt": "2026-04-22T10:00:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"quotes": rows},
            "metadata": {
                "object_type": "quotes",
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
        normalize_quote,
    )

    default_props = [
        "hs_title",
        "hs_status",
        "hs_expiration_date",
        "hs_quote_total_preference",
        "hs_terms",
        "hs_public_url_key",
        "hubspot_owner_id",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
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
            "quotes",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=default_props,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"quotes": rows},
                "metadata": {
                    "object_type": "quotes",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_quote(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"quotes": rows},
        "metadata": {
            "object_type": "quotes",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
