"""HubSpot feedback sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_feedback(
    schema_version: str = "hubspot.crm.feedback.v1",
    mock: bool = True,
) -> dict:
    """Sync NPS/CSAT submissions into a tables envelope."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_object,
    )

    if mock:
        base_props = {
            "hs_survey_type": "NPS",
            "hs_survey_name": "Tenant Satisfaction Q2",
            "hs_value": "9",
            "hs_response": "Great service - quick response on maintenance.",
            "hs_responder_email": "tenant@example.com",
            "hs_submission_timestamp": "2026-04-20T11:00:00Z",
        }
        rows = [
            normalize_object(
                {
                    "id": f"F{1000 + i}",
                    "properties": base_props,
                    "createdAt": "2026-04-20T11:00:00Z",
                    "updatedAt": "2026-04-20T11:00:00Z",
                    "archived": False,
                },
                object_type="feedback_submission",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"feedback": rows},
            "metadata": {
                "object_type": "feedback",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
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
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"feedback": rows},
                "metadata": {
                    "object_type": "feedback",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_object(r, object_type="feedback_submission")
            for r in body.get("results", [])
        )
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"feedback": rows},
        "metadata": {
            "object_type": "feedback",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
