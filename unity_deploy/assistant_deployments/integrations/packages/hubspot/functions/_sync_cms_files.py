"""HubSpot cms_files sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_cms_files(
    schema_version: str = "hubspot.cms.files.v1",
    mock: bool = True,
) -> dict:
    """Sync CMS file metadata into a tables envelope."""
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_file,
    )

    if mock:
        base = {
            "name": "sunset-tower-floorplan.pdf",
            "extension": "pdf",
            "type": "DOCUMENT",
            "size": 1234567,
            "url": "https://example.com/files/sunset-tower-floorplan.pdf",
            "alt": "Sunset Tower floor plan",
            "parentFolderId": None,
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T12:00:00Z",
        }
        rows = [normalize_file({**base, "id": f"fl-{1000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"cms_files": rows},
            "metadata": {
                "object_type": "cms_files",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/files/v3/files", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"cms_files": rows},
                "metadata": {
                    "object_type": "cms_files",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_file(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"cms_files": rows},
        "metadata": {
            "object_type": "cms_files",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
