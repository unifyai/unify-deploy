"""HubSpot Lists - contact lists (CRUD + membership)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_LIST = {
    "listId": "401",
    "name": "Q3 Prospective Tenants",
    "listType": "STATIC",
    "processingType": "MANUAL",
    "createdAt": "2026-04-01T10:00:00Z",
    "updatedAt": "2026-04-25T15:00:00Z",
}


@custom_function()
async def get_list(list_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_LIST, "listId": str(list_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/crm/v3/lists/{list_id}")


@custom_function()
async def list_lists(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    """List all contact lists in the portal."""
    if mock:
        return {
            "results": [{**_MOCK_LIST, "listId": str(400 + i)} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body = await hubspot_post(
        "/crm/v3/lists/search",
        {"count": min(limit, 250), "offset": int(after) if after else 0},
    )
    if "error" in body:
        return body
    return {"results": body.get("lists", []),
            "next_after": str(body.get("offset")) if body.get("hasMore") else None}


@custom_function()
async def create_list(
    name: str,
    list_type: str = "STATIC",
    processing_type: str = "MANUAL",
    mock: bool = True,
) -> dict:
    """Create a contact list.  ``list_type`` can be ``STATIC`` or ``DYNAMIC``."""
    if mock:
        return {**_MOCK_LIST, "listId": "99001", "name": name,
                "listType": list_type, "processingType": processing_type}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/crm/v3/lists",
        {"name": name, "objectTypeId": "0-1", "processingType": processing_type},
    )


@custom_function()
async def delete_list(list_id: str, mock: bool = True) -> dict:
    if mock:
        return {"status": "deleted", "list_id": str(list_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {"error": "Deletes are disabled.  Set HUBSPOT_ALLOW_DELETE=true.",
                "list_id": str(list_id)}
    return await hubspot_delete(f"/crm/v3/lists/{list_id}")


@custom_function()
async def add_contact_to_list(
    list_id: str,
    contact_id: str,
    mock: bool = True,
) -> dict:
    """Add a contact to a STATIC list.  No-op on DYNAMIC lists (they
    auto-populate from filters)."""
    if mock:
        return {"status": "added", "list_id": str(list_id), "contact_id": str(contact_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_put,
    )

    # /crm/v3/lists/{listId}/memberships/add (PUT) - implemented via PATCH semantics here.
    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/crm/v3/lists/{list_id}/memberships/add",
        [str(contact_id)],
    )


@custom_function()
async def remove_contact_from_list(
    list_id: str,
    contact_id: str,
    mock: bool = True,
) -> dict:
    if mock:
        return {"status": "removed", "list_id": str(list_id), "contact_id": str(contact_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/crm/v3/lists/{list_id}/memberships/remove",
        [str(contact_id)],
    )


@custom_function()
async def sync_lists(
    schema_version: str = "hubspot.crm.lists.v1",
    mock: bool = True,
) -> dict:
    """Sync list definitions and (small) memberships.  Heavy memberships
    should run as a separate scheduled task; v0 syncs definitions only."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_list,
    )

    if mock:
        rows = [normalize_list({**_MOCK_LIST, "listId": str(400 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"lists": rows, "list_membership": []},
            "metadata": {"object_type": "lists", "mode": "mock", "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    rows: list[dict] = []
    offset = 0
    while True:
        body = await hubspot_post(
            "/crm/v3/lists/search",
            {"count": 250, "offset": offset},
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"lists": rows, "list_membership": []},
                    "metadata": {"object_type": "lists", "mode": "real",
                                 "row_count": len(rows), "partial": True}}
        rows.extend(normalize_list(r) for r in body.get("lists", []))
        if not body.get("hasMore"):
            break
        offset = body.get("offset", offset + 250)

    return {
        "schema_version": schema_version,
        "tables": {"lists": rows, "list_membership": []},
        "metadata": {"object_type": "lists", "mode": "real", "row_count": len(rows)},
    }
