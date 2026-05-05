"""HubSpot Workflows - enrollment + read-only definitions (tier-gated)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_marketing_workflows(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """List marketing workflows."""
    if mock:
        base = {
            "name": "Prospective Tenant Nurture",
            "type": "DRIP_DELAY",
            "enabled": True,
            "createdAt": "2025-10-01T09:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }
        return {
            "results": [{**base, "id": f"wf-{3000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/automation/v4/flows", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def enroll_in_hubspot_workflow(
    workflow_id: str,
    contact_email: str,
    mock: bool = True,
) -> dict:
    """Enroll a contact in a workflow."""
    if mock:
        return {
            "status": "enrolled",
            "workflow_id": str(workflow_id),
            "contact_email": contact_email,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/automation/v2/workflows/{workflow_id}/enrollments/contacts/{contact_email}",
        {},
    )


@custom_function()
async def unenroll_from_hubspot_workflow(
    workflow_id: str,
    contact_email: str,
    mock: bool = True,
) -> dict:
    """Unenroll a contact from a workflow."""
    if mock:
        return {
            "status": "unenrolled",
            "workflow_id": str(workflow_id),
            "contact_email": contact_email,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )

    return await hubspot_delete(
        f"/automation/v2/workflows/{workflow_id}/enrollments/contacts/{contact_email}",
    )


@custom_function()
async def sync_hubspot_marketing_workflows(
    schema_version: str = "hubspot.marketing.workflows.v1",
    mock: bool = True,
) -> dict:
    """Sync marketing workflow definitions into a tables envelope."""
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_workflow,
    )

    if mock:
        base = {
            "name": "Prospective Tenant Nurture",
            "type": "DRIP_DELAY",
            "enabled": True,
            "createdAt": "2025-10-01T09:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }
        rows = [normalize_workflow({**base, "id": f"wf-{3000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_workflows": rows},
            "metadata": {
                "object_type": "marketing_workflows",
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
        body = await hubspot_get("/automation/v4/flows", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_workflows": rows},
                "metadata": {
                    "object_type": "marketing_workflows",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_workflow(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_workflows": rows},
        "metadata": {
            "object_type": "marketing_workflows",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
