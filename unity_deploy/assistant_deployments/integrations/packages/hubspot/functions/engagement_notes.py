"""HubSpot Engagements - Notes (create + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def create_note(
    body: str,
    timestamp_iso: str | None = None,
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Create a note attached to one or more CRM records."""
    if mock:
        return {
            "id": "N99001",
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": timestamp_iso or "2026-04-22T16:00:00Z",
                "hubspot_owner_id": "60001",
                "hs_object_id": "N99001",
                "hs_lastmodifieddate": "2026-04-22T16:00:00Z",
            },
            "createdAt": "2026-04-22T16:00:00Z",
            "updatedAt": "2026-04-22T16:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._engagement_helpers import (
        build_associations,
        now_ms_str,
    )

    body_payload: dict = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": timestamp_iso or now_ms_str(),
        },
    }
    if associations:
        type_ids = {"contact": 202, "company": 190, "deal": 214, "ticket": 228}
        body_payload["associations"] = build_associations(associations, type_ids)
    return await hubspot_post("/crm/v3/objects/notes", body_payload)


@custom_function()
async def list_notes(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot notes."""
    if mock:
        base_props = {
            "hs_note_body": "Owner prefers monthly statements via email; quarterly board updates.",
            "hs_timestamp": "2026-04-22T16:00:00Z",
            "hubspot_owner_id": "60001",
        }
        return {
            "results": [
                {
                    "id": f"N{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"N{1000 + i}"},
                    "createdAt": "2026-04-22T16:00:00Z",
                    "updatedAt": "2026-04-22T16:00:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "hs_note_body",
        "hs_timestamp",
        "hubspot_owner_id",
        "hs_object_id",
        "hs_lastmodifieddate",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/notes", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def sync_notes(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.notes.v1",
    mock: bool = True,
) -> dict:
    """Sync notes modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )

        base_props = {
            "hs_note_body": "Owner prefers monthly statements via email.",
            "hs_timestamp": "2026-04-22T16:00:00Z",
            "hubspot_owner_id": "60001",
        }
        rows = [
            normalize_engagement(
                {
                    "id": f"N{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"N{1000 + i}"},
                    "createdAt": "2026-04-22T16:00:00Z",
                    "updatedAt": "2026-04-22T16:00:00Z",
                    "archived": False,
                },
                engagement_type="note",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"notes": rows},
            "metadata": {
                "object_type": "notes",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_engagement,
    )

    default_props = [
        "hs_note_body",
        "hs_timestamp",
        "hubspot_owner_id",
        "hs_object_id",
        "hs_lastmodifieddate",
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
            "notes",
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
                "tables": {"notes": rows},
                "metadata": {
                    "object_type": "notes",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_engagement(r, engagement_type="note")
            for r in body.get("results", [])
        )
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"notes": rows},
        "metadata": {
            "object_type": "notes",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
