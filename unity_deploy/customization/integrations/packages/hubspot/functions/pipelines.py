"""HubSpot Pipelines - deal + ticket pipeline definitions (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def sync_pipelines(
    schema_version: str = "hubspot.crm.pipelines.v1",
    mock: bool = True,
) -> dict:
    """Sync pipeline + stage definitions for deals and tickets."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_pipeline_stage,
    )

    if mock:
        mock_pipelines = [
            {
                "id": "default",
                "label": "Sales Pipeline",
                "stages": [
                    {"id": "appointmentscheduled", "label": "Appointment Scheduled",
                     "displayOrder": 0, "metadata": {"probability": "0.2"}, "archived": False},
                    {"id": "qualifiedtobuy", "label": "Qualified to Buy",
                     "displayOrder": 1, "metadata": {"probability": "0.4"}, "archived": False},
                    {"id": "presentationscheduled", "label": "Presentation Scheduled",
                     "displayOrder": 2, "metadata": {"probability": "0.6"}, "archived": False},
                    {"id": "decisionmakerboughtin", "label": "Decision Maker Bought-In",
                     "displayOrder": 3, "metadata": {"probability": "0.8"}, "archived": False},
                    {"id": "contractsent", "label": "Contract Sent",
                     "displayOrder": 4, "metadata": {"probability": "0.9"}, "archived": False},
                    {"id": "closedwon", "label": "Closed Won",
                     "displayOrder": 5, "metadata": {"probability": "1.0"}, "archived": False},
                    {"id": "closedlost", "label": "Closed Lost",
                     "displayOrder": 6, "metadata": {"probability": "0.0"}, "archived": False},
                ],
            },
        ]
        rows = []
        for pipe in mock_pipelines:
            rows.extend(
                normalize_pipeline_stage(pipe, st, object_type="deals")
                for st in pipe["stages"]
            )
        return {
            "schema_version": schema_version,
            "tables": {"pipelines": rows},
            "metadata": {"object_type": "pipelines", "mode": "mock", "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    errors: list[dict] = []
    for object_type in ("deals", "tickets"):
        body = await hubspot_get(f"/crm/v3/pipelines/{object_type}")
        if "error" in body:
            errors.append({"object_type": object_type, "error": body["error"]})
            continue
        for pipe in body.get("results", []):
            for stage in pipe.get("stages", []):
                rows.append(normalize_pipeline_stage(pipe, stage, object_type=object_type))

    return {
        "schema_version": schema_version,
        "tables": {"pipelines": rows},
        "metadata": {"object_type": "pipelines", "mode": "real",
                     "row_count": len(rows), "errors": errors,
                     "partial": bool(errors)},
    }
