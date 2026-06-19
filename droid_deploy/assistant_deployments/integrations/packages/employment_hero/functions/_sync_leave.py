"""Employment Hero leave sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_leave(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot leave categories, balances, and requests."""
    import datetime as _dt

    schema_version = "employment-hero.leave.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "leave_categories": [
                    {
                        "id": "lc-1",
                        "name": "Annual Leave",
                        "is_paid": True,
                        "country": "GB",
                    },
                ],
                "leave_balances": [
                    {
                        "employee_id": "emp-mock-1",
                        "category_id": "lc-1",
                        "balance_key": "emp-mock-1:lc-1",
                        "balance_hours": 168.0,
                        "accrued_hours": 224.0,
                    },
                ],
                "leave_requests": [
                    {
                        "id": "lr-mock-1",
                        "employee_id": "emp-mock-1",
                        "category_id": "lc-1",
                        "start_date": "2026-05-12",
                        "end_date": "2026-05-16",
                        "status": "approved",
                        "total_hours": 32.0,
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "leave",
                "started_at": started,
                "mode": "mock",
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        org_path,
        _org_id_or_error,
    )
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )

    org_id, err = _org_id_or_error()
    if err is not None:
        err.update({"schema_version": schema_version, "tables": {}})
        return err
    cfg = get_employmenthero_config()

    cats_raw = await eh_paginate(
        org_path("/leave_categories"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    leave_categories = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "is_paid": c.get("is_paid"),
            "accrual_rule": c.get("accrual_rule"),
            "country": c.get("country"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        for c in cats_raw
    ]

    balances_raw = await eh_paginate(
        org_path("/leave_balances"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    leave_balances = [
        {
            "balance_key": f"{b.get('employee_id')}:{b.get('category_id')}",
            "employee_id": b.get("employee_id"),
            "category_id": b.get("category_id"),
            "balance_hours": b.get("balance_hours"),
            "accrued_hours": b.get("accrued_hours"),
            "as_of": b.get("as_of") or b.get("updated_at"),
        }
        for b in balances_raw
    ]

    req_params: dict = {}
    if since:
        req_params["updated_since"] = since
    requests_raw = await eh_paginate(
        org_path("/leave_requests"),
        params=req_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    leave_requests = [
        {
            "id": r.get("id"),
            "employee_id": r.get("employee_id"),
            "category_id": r.get("category_id"),
            "start_date": r.get("start_date"),
            "end_date": r.get("end_date"),
            "status": r.get("status"),
            "total_hours": r.get("total_hours"),
            "approved_by": r.get("approved_by"),
            "approved_at": r.get("approved_at"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        for r in requests_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "leave_categories": leave_categories,
            "leave_balances": leave_balances,
            "leave_requests": leave_requests,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "leave",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "leave_categories": len(leave_categories),
                "leave_balances": len(leave_balances),
                "leave_requests": len(leave_requests),
            },
        },
    }
