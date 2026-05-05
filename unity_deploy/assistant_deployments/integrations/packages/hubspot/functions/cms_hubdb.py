"""HubSpot HubDB - structured tables (rows CRUD + table publish)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_hubdb_tables(mock: bool = True) -> dict:
    """List all HubDB table definitions."""
    if mock:
        base = {
            "name": "properties",
            "label": "Properties Catalog",
            "columns": [
                {"name": "name", "label": "Name", "type": "TEXT"},
                {"name": "address", "label": "Address", "type": "TEXT"},
                {"name": "unit_count", "label": "Units", "type": "NUMBER"},
            ],
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
            "publishedAt": "2026-04-15T12:00:00Z",
        }
        return {"results": [{**base, "id": str(11000 + i)} for i in range(2)]}

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/cms/v3/hubdb/tables")
    if "error" in body:
        return body
    return {"results": body.get("results", [])}


@custom_function()
async def get_hubspot_hubdb_table(table_id: str, mock: bool = True) -> dict:
    """Fetch a HubDB table definition by ID."""
    if mock:
        return {
            "id": str(table_id),
            "name": "properties",
            "label": "Properties Catalog",
            "columns": [
                {"name": "name", "label": "Name", "type": "TEXT"},
                {"name": "address", "label": "Address", "type": "TEXT"},
                {"name": "unit_count", "label": "Units", "type": "NUMBER"},
            ],
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
            "publishedAt": "2026-04-15T12:00:00Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/cms/v3/hubdb/tables/{table_id}")


@custom_function()
async def list_hubspot_hubdb_rows(
    table_id: str,
    after: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Paginate through rows in a HubDB table."""
    if mock:
        base = {
            "values": {
                "name": "Sunset Tower",
                "address": "100 Main St",
                "unit_count": 120,
            },
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }
        return {
            "table_id": str(table_id),
            "results": [{**base, "id": str(21000 + i)} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 1000)}
    if after:
        params["after"] = after
    body = await hubspot_get(f"/cms/v3/hubdb/tables/{table_id}/rows", params=params)
    if "error" in body:
        return body
    return {
        "table_id": str(table_id),
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_hubdb_row(
    table_id: str, values: dict, mock: bool = True
) -> dict:
    """Insert a draft row into a HubDB table."""
    if mock:
        return {
            "id": "29001",
            "values": values,
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/cms/v3/hubdb/tables/{table_id}/rows",
        {"values": values},
    )


@custom_function()
async def update_hubspot_hubdb_row(
    table_id: str,
    row_id: str,
    values: dict,
    mock: bool = True,
) -> dict:
    """Patch a HubDB row's values (draft state)."""
    if mock:
        return {
            "id": str(row_id),
            "values": values,
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/cms/v3/hubdb/tables/{table_id}/rows/{row_id}/draft",
        {"values": values},
    )


@custom_function()
async def delete_hubspot_hubdb_row(
    table_id: str, row_id: str, mock: bool = True
) -> dict:
    """Delete a HubDB row.  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "table_id": str(table_id), "row_id": str(row_id)}

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {
            "error": "Deletes disabled.  Set HUBSPOT_ALLOW_DELETE=true.",
            "table_id": str(table_id),
            "row_id": str(row_id),
        }
    return await hubspot_delete(f"/cms/v3/hubdb/tables/{table_id}/rows/{row_id}/draft")


@custom_function()
async def publish_hubspot_hubdb_table(
    table_id: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Publish HubDB table draft.  HIGH-STAKES (changes go live).
    Requires confirm=True + HUBSPOT_ALLOW_CMS_PUBLISH=true."""
    if not confirm:
        return {
            "error": "publish_hubspot_hubdb_table requires confirm=True.",
            "table_id": str(table_id),
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_cms_publish"]:
        return {
            "error": "CMS publish disabled.  Set HUBSPOT_ALLOW_CMS_PUBLISH=true.",
            "table_id": str(table_id),
        }

    if mock:
        return {"status": "published", "table_id": str(table_id)}

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(f"/cms/v3/hubdb/tables/{table_id}/draft/publish", {})


@custom_function()
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
