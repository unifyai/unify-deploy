"""HubSpot cms_hubdb sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_hubdb(
    schema_version: str = "hubspot.cms.hubdb.v1",
    mock: bool = True,
) -> dict:
    """Sync HubDB tables + rows.  Each row's ``values`` dict is flattened
    into the row dict alongside its identifier columns."""
    if mock:
        mock_table = {
            "name": "properties",
            "label": "Properties Catalog",
            "columns": [
                {"name": "name", "label": "Name", "type": "TEXT"},
                {"name": "address", "label": "Address", "type": "TEXT"},
                {"name": "unit_count", "label": "Units", "type": "NUMBER"},
            ],
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
        }
        mock_row_values = {
            "name": "Sunset Tower",
            "address": "100 Main St",
            "unit_count": 120,
        }
        tables_rows = [
            {
                "table_id": str(11000 + i),
                "name": mock_table["name"],
                "label": mock_table["label"],
                "columns_json": str(mock_table["columns"]),
                "created_at": mock_table["createdAt"],
                "updated_at": mock_table["updatedAt"],
            }
            for i in range(2)
        ]
        rows = [
            {
                "table_id": "11001",
                "row_id": str(21000 + j),
                **mock_row_values,
                "created_at": "2026-01-15T10:00:00Z",
                "updated_at": "2026-04-01T11:00:00Z",
            }
            for j in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"hubdb_tables": tables_rows, "hubdb_rows": rows},
            "metadata": {
                "object_type": "hubdb",
                "mode": "mock",
                "tables": len(tables_rows),
                "rows": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    tables_body = await hubspot_get("/cms/v3/hubdb/tables")
    if "error" in tables_body:
        return {
            "schema_version": schema_version,
            "error": tables_body["error"],
            "tables": {"hubdb_tables": [], "hubdb_rows": []},
            "metadata": {
                "object_type": "hubdb",
                "mode": "real",
                "tables": 0,
                "rows": 0,
                "partial": True,
            },
        }

    tables_rows: list[dict] = []
    rows: list[dict] = []
    for t in tables_body.get("results", []):
        tables_rows.append(
            {
                "table_id": str(t.get("id", "")),
                "name": t.get("name", ""),
                "label": t.get("label", ""),
                "columns_json": str(t.get("columns", [])),
                "created_at": t.get("createdAt", ""),
                "updated_at": t.get("updatedAt", ""),
            },
        )
        after: str | None = None
        while True:
            params: dict = {"limit": 1000}
            if after:
                params["after"] = after
            row_body = await hubspot_get(
                f"/cms/v3/hubdb/tables/{t['id']}/rows",
                params=params,
            )
            if "error" in row_body:
                break
            for r in row_body.get("results", []):
                row = {
                    "table_id": str(t["id"]),
                    "row_id": str(r.get("id", "")),
                    "created_at": r.get("createdAt", ""),
                    "updated_at": r.get("updatedAt", ""),
                }
                row.update(r.get("values") or {})
                rows.append(row)
            after = row_body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break

    return {
        "schema_version": schema_version,
        "tables": {"hubdb_tables": tables_rows, "hubdb_rows": rows},
        "metadata": {
            "object_type": "hubdb",
            "mode": "real",
            "tables": len(tables_rows),
            "rows": len(rows),
        },
    }
