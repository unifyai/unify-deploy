"""HubSpot Feedback Submissions - NPS / CSAT data (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_FEEDBACK = {
    "id": "F1001",
    "properties": {
        "hs_survey_type": "NPS",
        "hs_survey_name": "Tenant Satisfaction Q2",
        "hs_value": "9",
        "hs_response": "Great service - quick response on maintenance.",
        "hs_responder_email": "tenant@example.com",
        "hs_submission_timestamp": "2026-04-20T11:00:00Z",
    },
    "createdAt": "2026-04-20T11:00:00Z",
    "updatedAt": "2026-04-20T11:00:00Z",
    "archived": False,
}


@custom_function()
async def list_feedback_submissions(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_FEEDBACK, "id": f"F{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/feedback_submissions", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_feedback(
    schema_version: str = "hubspot.crm.feedback.v1",
    mock: bool = True,
) -> dict:
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_object,
    )

    if mock:
        rows = [normalize_object({**_MOCK_FEEDBACK, "id": f"F{1000 + i}"},
                                 object_type="feedback_submission") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"feedback": rows},
            "metadata": {"object_type": "feedback", "mode": "mock", "row_count": len(rows)},
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
        body = await hubspot_get("/crm/v3/objects/feedback_submissions", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"feedback": rows},
                    "metadata": {"object_type": "feedback", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_object(r, object_type="feedback_submission")
                    for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"feedback": rows},
        "metadata": {"object_type": "feedback", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
