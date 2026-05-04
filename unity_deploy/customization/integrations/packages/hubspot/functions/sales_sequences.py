"""HubSpot Sales Sequences - outbound cadences (tier-gated, Sales Hub Pro+)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_SEQUENCE = {
    "id": "seq-9001",
    "name": "Owner Outreach - Discovery",
    "folderId": None,
    "createdAt": "2025-09-15T10:00:00Z",
    "updatedAt": "2026-01-15T11:00:00Z",
}


@custom_function()
async def list_sequences(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_SEQUENCE, "id": f"seq-{9000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/automation/v4/sequences", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_sequence(sequence_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_SEQUENCE, "id": str(sequence_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/automation/v4/sequences/{sequence_id}")


@custom_function()
async def enroll_in_sequence(
    sequence_id: str,
    contact_email: str,
    sender_email: str | None = None,
    mock: bool = True,
) -> dict:
    """Enroll a contact in a sequence.  Tier-gated (Sales Hub Pro+)."""
    if mock:
        return {"status": "enrolled", "sequence_id": str(sequence_id),
                "contact_email": contact_email}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body: dict = {"sequenceId": sequence_id, "email": contact_email}
    if sender_email:
        body["senderEmail"] = sender_email
    return await hubspot_post("/automation/v4/sequences/enrollments", body)


@custom_function()
async def unenroll_from_sequence(
    sequence_id: str,
    contact_email: str,
    mock: bool = True,
) -> dict:
    if mock:
        return {"status": "unenrolled", "sequence_id": str(sequence_id),
                "contact_email": contact_email}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/automation/v4/sequences/{sequence_id}/enrollments/email/{contact_email}/cancel",
        {},
    )


@custom_function()
async def sync_sequences(
    schema_version: str = "hubspot.sales.sequences.v1",
    mock: bool = True,
) -> dict:
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_sequence,
    )

    if mock:
        rows = [normalize_sequence({**_MOCK_SEQUENCE, "id": f"seq-{9000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"sales_sequences": rows},
            "metadata": {"object_type": "sales_sequences", "mode": "mock",
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
        body = await hubspot_get("/automation/v4/sequences", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"sales_sequences": rows},
                    "metadata": {"object_type": "sales_sequences", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_sequence(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"sales_sequences": rows},
        "metadata": {"object_type": "sales_sequences", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
