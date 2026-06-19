"""HubSpot cms_domains sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


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

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
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
