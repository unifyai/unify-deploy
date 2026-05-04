"""HubSpot CMS - Files (list/get/upload/delete)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_cms_files(
    after: str | None = None,
    limit: int = 50,
    parent_folder_id: int | None = None,
    mock: bool = True,
) -> dict:
    """Paginate through CMS files."""
    if mock:
        base = {
            "name": "sunset-tower-floorplan.pdf",
            "extension": "pdf", "type": "DOCUMENT", "size": 1234567,
            "url": "https://example.com/files/sunset-tower-floorplan.pdf",
            "alt": "Sunset Tower floor plan",
            "parentFolderId": None,
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T12:00:00Z",
        }
        return {
            "results": [{**base, "id": f"fl-{1000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    if parent_folder_id is not None:
        params["parentFolderId"] = parent_folder_id
    body = await hubspot_get("/files/v3/files", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_cms_file(file_id: str, mock: bool = True) -> dict:
    """Fetch a CMS file by ID."""
    if mock:
        return {
            "id": str(file_id),
            "name": "sunset-tower-floorplan.pdf",
            "extension": "pdf", "type": "DOCUMENT", "size": 1234567,
            "url": "https://example.com/files/sunset-tower-floorplan.pdf",
            "alt": "Sunset Tower floor plan",
            "parentFolderId": None,
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T12:00:00Z",
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/files/v3/files/{file_id}")


@custom_function()
async def upload_cms_file(
    file_path: str,
    folder_path: str = "/",
    access: str = "PUBLIC_INDEXABLE",
    mock: bool = True,
) -> dict:
    """Upload a local file to the HubSpot Files library."""
    if mock:
        return {
            "id": "fl-99001",
            "name": file_path.rsplit("/", 1)[-1],
            "extension": "pdf", "type": "DOCUMENT", "size": 1234567,
            "url": "https://example.com/files/uploaded.pdf",
            "createdAt": "2026-01-15T10:00:00Z",
            "updatedAt": "2026-04-01T12:00:00Z",
        }

    import os
    import httpx

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        _token_or_none,
    )

    token = _token_or_none()
    if token is None:
        return {"error": "HUBSPOT_PRIVATE_APP_TOKEN is not configured."}

    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    options = {"access": access, "ttl": "P3M", "overwrite": False, "duplicateValidationStrategy": "NONE",
               "duplicateValidationScope": "ENTIRE_PORTAL"}
    timeout = float(os.environ.get("HUBSPOT_REQUEST_TIMEOUT_SECONDS", "30"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        with open(file_path, "rb") as fh:
            files = {"file": (os.path.basename(file_path), fh)}
            data = {
                "folderPath": folder_path,
                "options": str(options).replace("'", '"'),
            }
            resp = await client.post(
                "https://api.hubapi.com/files/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                data=data,
            )
    if resp.status_code in (200, 201):
        return resp.json()
    return {"error": f"Upload failed: {resp.status_code}", "body": resp.text[:500]}


@custom_function()
async def delete_cms_file(file_id: str, mock: bool = True) -> dict:
    """Delete a CMS file.  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "id": str(file_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {"error": "Deletes disabled.  Set HUBSPOT_ALLOW_DELETE=true.",
                "id": str(file_id)}
    return await hubspot_delete(f"/files/v3/files/{file_id}")


@custom_function()
async def sync_cms_files(
    schema_version: str = "hubspot.cms.files.v1",
    mock: bool = True,
) -> dict:
    """Sync CMS file metadata into a tables envelope."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_file,
    )

    if mock:
        base = {
            "name": "sunset-tower-floorplan.pdf",
            "extension": "pdf", "type": "DOCUMENT", "size": 1234567,
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
            "metadata": {"object_type": "cms_files", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
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
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"cms_files": rows},
                    "metadata": {"object_type": "cms_files", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_file(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"cms_files": rows},
        "metadata": {"object_type": "cms_files", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
