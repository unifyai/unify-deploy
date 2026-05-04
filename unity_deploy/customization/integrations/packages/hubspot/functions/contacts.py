"""HubSpot Contacts - on-demand CRUD + sync.

The canonical CRM object.  Other CRM objects (companies, deals, tickets)
follow this file's structure.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "firstname", "lastname", "email", "phone", "company", "jobtitle",
    "lifecyclestage", "hs_lead_status", "createdate", "lastmodifieddate",
    "hs_object_id",
]

_MOCK_CONTACT = {
    "id": "12345",
    "properties": {
        "firstname": "Sample", "lastname": "Contact",
        "email": "sample.contact@example.com",
        "phone": "+1 555 123 4567",
        "company": "Acme Properties LLC",
        "jobtitle": "Property Owner",
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
        "createdate": "2026-04-01T10:00:00Z",
        "lastmodifieddate": "2026-04-15T14:30:00Z",
        "hs_object_id": "12345",
    },
    "createdAt": "2026-04-01T10:00:00Z",
    "updatedAt": "2026-04-15T14:30:00Z",
    "archived": False,
}


@custom_function()
async def get_contact(contact_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot contact by ID.  Returns the raw HubSpot record.

    Parameters
    ----------
    contact_id : str
        HubSpot contact ID (numeric string).
    mock : bool
        If True, returns deterministic fixture data without hitting the API.
    """
    if mock:
        return {**_MOCK_CONTACT, "id": str(contact_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    cfg = get_hubspot_config()
    properties = (
        ",".join(cfg["contact_properties"])
        if isinstance(cfg["contact_properties"], list)
        else ",".join(_DEFAULT_PROPERTIES)
    )
    return await hubspot_get(
        f"/crm/v3/objects/contacts/{contact_id}",
        params={"properties": properties, "archived": "false"},
    )


@custom_function()
async def search_contacts(query: str, limit: int = 10, mock: bool = True) -> dict:
    """Full-text search HubSpot contacts.  Searches firstname, lastname,
    email, company by default.

    Returns ``{"results": [...], "total": int}``.
    """
    if mock:
        return {
            "results": [
                {**_MOCK_CONTACT, "id": "12345"},
                {**_MOCK_CONTACT, "id": "67890",
                 "properties": {**_MOCK_CONTACT["properties"],
                                "firstname": "Another", "email": "another@example.com"}},
            ],
            "total": 2,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    return await hubspot_search(
        "contacts",
        query=query,
        properties=_DEFAULT_PROPERTIES,
        limit=min(limit, 100),
    )


@custom_function()
async def list_contacts(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through all HubSpot contacts.  Returns
    ``{"results": [...], "next_after": str | None}``."""
    if mock:
        return {
            "results": [{**_MOCK_CONTACT, "id": str(10000 + i)} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {
        "limit": min(limit, 100),
        "properties": ",".join(_DEFAULT_PROPERTIES),
    }
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/contacts", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_contact(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot contact.  ``properties`` is a flat dict of HubSpot
    property names -> values.  At minimum HubSpot expects ``email``."""
    if mock:
        return {
            **_MOCK_CONTACT,
            "id": "99999",
            "properties": {**_MOCK_CONTACT["properties"], **properties},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/crm/v3/objects/contacts",
        {"properties": properties},
    )


@custom_function()
async def update_contact(contact_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch HubSpot contact properties.  Only sends the keys provided."""
    if mock:
        return {
            **_MOCK_CONTACT,
            "id": str(contact_id),
            "properties": {**_MOCK_CONTACT["properties"], **properties},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/contacts/{contact_id}",
        {"properties": properties},
    )


@custom_function()
async def delete_contact(contact_id: str, mock: bool = True) -> dict:
    """Soft-delete a HubSpot contact (archive).  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "id": str(contact_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {
            "error": "Deletes are disabled.  Set HUBSPOT_ALLOW_DELETE=true on the assistant to enable.",
            "id": str(contact_id),
        }
    return await hubspot_delete(f"/crm/v3/objects/contacts/{contact_id}")


@custom_function()
async def sync_contacts(
    since: str | None = None,
    schema_version: str = "hubspot.crm.contacts.v1",
    mock: bool = True,
) -> dict:
    """Pull contacts modified since ``since`` (ISO-8601) into the
    ``{schema_version, tables, metadata}`` envelope used by the scenario
    runtime.  Returns ``tables['contacts']`` flattened for DataManager."""
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_contact,
        )
        rows = [normalize_contact({**_MOCK_CONTACT, "id": str(10000 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"contacts": rows},
            "metadata": {"object_type": "contacts", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_contact,
    )

    cfg = get_hubspot_config()
    properties = (
        cfg["contact_properties"]
        if isinstance(cfg["contact_properties"], list)
        else _DEFAULT_PROPERTIES
    )
    filter_groups = (
        [{"filters": [{
            "propertyName": "hs_lastmodifieddate",
            "operator": "GT",
            "value": since,
        }]}]
        if since else []
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
                "metadata": {"object_type": "contacts", "mode": "real",
                             "since": since, "row_count": len(rows),
                             "pages": page_count, "partial": True},
            }
        rows.extend(normalize_contact(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break

    return {
        "schema_version": schema_version,
        "tables": {"contacts": rows},
        "metadata": {"object_type": "contacts", "mode": "real",
                     "since": since, "row_count": len(rows),
                     "pages": page_count},
    }
