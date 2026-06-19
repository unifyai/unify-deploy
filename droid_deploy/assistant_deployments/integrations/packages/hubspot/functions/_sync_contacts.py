"""HubSpot contacts sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_contacts(
    since: str | None = None,
    schema_version: str = "hubspot.crm.contacts.v1",
    mock: bool = True,
) -> dict:
    """Pull contacts modified since ``since`` (ISO-8601) into the
    ``{schema_version, tables, metadata}`` envelope used by the scenario
    runtime.  Returns ``tables['contacts']`` flattened for DataManager."""
    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_contact,
        )

        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        rows = [
            normalize_contact(
                {
                    "id": str(10000 + i),
                    "properties": {**base_props, "hs_object_id": str(10000 + i)},
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"contacts": rows},
            "metadata": {
                "object_type": "contacts",
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
        normalize_contact,
    )

    default_props = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
        "hs_lead_status",
        "createdate",
        "lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["contact_properties"]
        if isinstance(cfg["contact_properties"], list)
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

    rows: list[dict] = []
    page_count = 0
    after: str | None = None
    while True:
        body = await hubspot_search(
            "contacts",
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
                "tables": {"contacts": rows},
                "metadata": {
                    "object_type": "contacts",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_contact(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break

    return {
        "schema_version": schema_version,
        "tables": {"contacts": rows},
        "metadata": {
            "object_type": "contacts",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
