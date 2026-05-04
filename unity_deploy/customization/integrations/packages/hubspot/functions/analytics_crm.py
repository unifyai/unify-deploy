"""HubSpot CRM Analytics - deal velocity + pipeline funnel + reports."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_deal_velocity_report(
    pipeline_id: str = "default",
    period_days: int = 90,
    mock: bool = True,
) -> dict:
    """Return aggregate deal-velocity metrics for a pipeline."""
    if mock:
        return {
            "pipeline_id": pipeline_id, "period_days": period_days,
            "summary": {
                "deals_closed_won": 42, "deals_closed_lost": 18,
                "avg_days_to_close": 38.5, "win_rate": 0.70,
                "total_revenue": 2_400_000,
            },
            "by_stage": [
                {"stage_id": "appointmentscheduled", "avg_days_in_stage": 4.2},
                {"stage_id": "qualifiedtobuy", "avg_days_in_stage": 6.1},
                {"stage_id": "presentationscheduled", "avg_days_in_stage": 8.3},
                {"stage_id": "decisionmakerboughtin", "avg_days_in_stage": 12.0},
                {"stage_id": "contractsent", "avg_days_in_stage": 7.9},
            ],
        }

    return {
        "pipeline_id": pipeline_id,
        "note": ("Production implementation aggregates from the synced "
                 "HubSpot/CRM/Dimensions/Deals + DealStageHistory contexts; "
                 "v0 returns mock data only."),
    }


@custom_function()
async def get_pipeline_funnel_report(
    pipeline_id: str = "default",
    mock: bool = True,
) -> dict:
    """Return current deal counts per stage."""
    if mock:
        return {
            "pipeline_id": pipeline_id,
            "stages": [
                {"stage_id": "appointmentscheduled", "label": "Appointment Scheduled",
                 "deal_count": 28, "pipeline_amount": 480000},
                {"stage_id": "qualifiedtobuy", "label": "Qualified to Buy",
                 "deal_count": 22, "pipeline_amount": 396000},
                {"stage_id": "presentationscheduled", "label": "Presentation Scheduled",
                 "deal_count": 18, "pipeline_amount": 324000},
                {"stage_id": "decisionmakerboughtin", "label": "Decision Maker Bought-In",
                 "deal_count": 12, "pipeline_amount": 264000},
                {"stage_id": "contractsent", "label": "Contract Sent",
                 "deal_count": 7, "pipeline_amount": 175000},
            ],
        }

    return {
        "pipeline_id": pipeline_id,
        "note": ("Production implementation aggregates from the synced "
                 "HubSpot/CRM/Dimensions/Deals context; v0 returns mock data only."),
    }


@custom_function()
async def list_crm_reports(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    """List custom CRM reports defined in the portal (tier-gated)."""
    if mock:
        return {"results": [
            {"id": "rpt-1", "name": "Q3 Owner Acquisition", "type": "CRM"},
            {"id": "rpt-2", "name": "Lease Conversion by Property", "type": "CRM"},
        ], "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/reports/v1/reports", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def run_crm_report(report_id: str, mock: bool = True) -> dict:
    """Run a saved CRM report and return its result rows."""
    if mock:
        return {"report_id": str(report_id),
                "rows": [
                    {"label": "Tampa", "value": 12},
                    {"label": "Miami", "value": 8},
                    {"label": "Orlando", "value": 5},
                ]}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get(f"/reports/v1/reports/{report_id}")
    return body
