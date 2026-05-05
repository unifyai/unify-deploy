"""HubSpot CMS - Domain list (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_cms_domains(mock: bool = True) -> dict:
    """List configured CMS domains."""
    if mock:
        base = {
            "domain": "example.com",
            "primarySite": True,
            "manuallyMarkedAsResolving": True,
            "createdAt": "2025-08-01T10:00:00Z",
            "updatedAt": "2026-01-01T12:00:00Z",
        }
        return {"results": [{**base, "id": f"dom-{13000 + i}"} for i in range(2)]}

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/cms/v3/domains")
    if "error" in body:
        return body
    return {"results": body.get("results", [])}


@custom_function()
async def sync_hubspot_cms_domains(
    schema_version: str = "hubspot.cms.domains.v1",
    mock: bool = True,
) -> dict:
    """Sync configured CMS domains into a tables envelope."""
    if mock:
        rows = [
            {
                "domain": "example.com",
                "primary_site": True,
                "created_at": "2025-08-01T10:00:00Z",
                "updated_at": "2026-01-01T12:00:00Z",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"cms_domains": rows},
            "metadata": {
                "object_type": "cms_domains",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/cms/v3/domains")
    if "error" in body:
        return {
            "schema_version": schema_version,
            "error": body["error"],
            "tables": {"cms_domains": []},
            "metadata": {
                "object_type": "cms_domains",
                "mode": "real",
                "row_count": 0,
                "partial": True,
            },
        }
    rows = []
    for r in body.get("results", []):
        rows.append(
            {
                "domain": r.get("domain", ""),
                "primary_site": bool(r.get("primarySite", False)),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            },
        )
    return {
        "schema_version": schema_version,
        "tables": {"cms_domains": rows},
        "metadata": {
            "object_type": "cms_domains",
            "mode": "real",
            "row_count": len(rows),
        },
    }
