"""Employment Hero leave management: categories, balances, requests."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@custom_function()
async def list_employmenthero_leave_categories(mock: bool = True) -> dict:
    """List leave categories (annual, sick, maternity, etc.) for the org."""
    if mock:
        return {
            "categories": [
                {
                    "id": "lc-1",
                    "name": "Annual Leave",
                    "is_paid": True,
                    "accrual_rule": "statutory_uk_28_days",
                    "country": "GB",
                },
                {
                    "id": "lc-2",
                    "name": "Sick Leave (SSP)",
                    "is_paid": True,
                    "accrual_rule": "uk_ssp",
                    "country": "GB",
                },
                {
                    "id": "lc-3",
                    "name": "Maternity Leave (SMP)",
                    "is_paid": True,
                    "accrual_rule": "uk_smp",
                    "country": "GB",
                },
                {
                    "id": "lc-4",
                    "name": "Unpaid Leave",
                    "is_paid": False,
                    "accrual_rule": None,
                    "country": "GB",
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/leave_categories"))
    if "error" in body:
        return body
    return {"categories": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_leave_category(category_id: str, mock: bool = True) -> dict:
    """Get one leave category by id."""
    if mock:
        return {
            "id": str(category_id),
            "name": "Annual Leave",
            "is_paid": True,
            "accrual_rule": "statutory_uk_28_days",
            "country": "GB",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/leave_categories/{category_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


@custom_function()
async def list_employmenthero_leave_balances(
    employee_id: str | None = None,
    mock: bool = True,
) -> dict:
    """List leave balances; if ``employee_id`` is supplied, only that employee."""
    if mock:
        return {
            "balances": [
                {
                    "employee_id": "emp-mock-1",
                    "category_id": "lc-1",
                    "balance_hours": 168.0,
                    "accrued_hours": 224.0,
                    "category_name": "Annual Leave",
                },
                {
                    "employee_id": "emp-mock-1",
                    "category_id": "lc-2",
                    "balance_hours": 280.0,
                    "accrued_hours": 280.0,
                    "category_name": "Sick Leave (SSP)",
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    if employee_id:
        path = org_path(f"/employees/{employee_id}/leave_balances")
    else:
        path = org_path("/leave_balances")
    body = await eh_get(path)
    if "error" in body:
        return body
    return {"balances": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_leave_balance(
    employee_id: str,
    category_id: str | None = None,
    mock: bool = True,
) -> dict:
    """Get an employee's leave balance, optionally for a single category."""
    if mock:
        if category_id:
            return {
                "employee_id": employee_id,
                "category_id": category_id,
                "balance_hours": 168.0,
                "accrued_hours": 224.0,
            }
        return {
            "employee_id": employee_id,
            "balances": [
                {
                    "category_id": "lc-1",
                    "balance_hours": 168.0,
                    "category_name": "Annual Leave",
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/leave_balances"))
    if "error" in body:
        return body
    rows = body.get("data") or body.get("items") or []
    if category_id:
        for r in rows:
            if r.get("category_id") == category_id:
                return r
        return {"error": f"No balance found for category {category_id}"}
    return {"employee_id": employee_id, "balances": rows}


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@custom_function()
async def list_employmenthero_leave_requests(
    employee_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """List leave requests with optional filters."""
    if mock:
        return {
            "requests": [
                {
                    "id": "lr-mock-1",
                    "employee_id": "emp-mock-1",
                    "category_id": "lc-1",
                    "start_date": "2026-05-12",
                    "end_date": "2026-05-16",
                    "status": "approved",
                    "total_hours": 32.0,
                    "notes": None,
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {"limit": min(limit, 100)}
    if employee_id:
        params["employee_id"] = employee_id
    if status:
        params["status"] = status
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    body = await eh_get(org_path("/leave_requests"), params=params)
    if "error" in body:
        return body
    return {"requests": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_leave_request(request_id: str, mock: bool = True) -> dict:
    """Get one leave request by id."""
    if mock:
        return {
            "id": str(request_id),
            "employee_id": "emp-mock-1",
            "category_id": "lc-1",
            "start_date": "2026-05-12",
            "end_date": "2026-05-16",
            "status": "approved",
            "total_hours": 32.0,
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/leave_requests/{request_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def submit_employmenthero_leave_request(
    employee_id: str,
    category_id: str,
    start_date: str,
    end_date: str,
    notes: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Submit a leave request on behalf of an employee.

    HIGH-STAKES WRITE.  Returns a refusal envelope unless ``confirm=True``
    AND ``EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true``.  The assistant
    must confirm the parameters with the user before invoking with
    ``confirm=True``.
    """
    if mock:
        return {
            "id": "lr-mock-new",
            "employee_id": employee_id,
            "category_id": category_id,
            "start_date": start_date,
            "end_date": end_date,
            "status": "pending",
            "_mocked": True,
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_post,
        org_path,
        _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )

    cfg = get_employmenthero_config()
    if not cfg["allow_high_stakes_writes"]:
        return {
            "error": "Refused: EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES is not true.",
            "hint": (
                "Tell the user this write is gated by a deployment flag.  "
                "Ask them to set EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true "
                "via Console -> Secrets, then retry."
            ),
        }
    if not confirm:
        return {
            "error": "Refused: confirm=True is required for live mutations.",
            "hint": (
                "Confirm the leave parameters with the user "
                "(employee, category, dates, notes) and retry with confirm=True."
            ),
        }

    _, err = _org_id_or_error()
    if err is not None:
        return err
    payload: dict = {
        "employee_id": employee_id,
        "category_id": category_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    if notes:
        payload["notes"] = notes
    body = await eh_post(org_path("/leave_requests"), payload)
    if "error" in body:
        return body
    created = body.get("data") or body
    if cfg["mirror_mutations_to_datamanager"]:
        from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_helpers import (
            mirror_leave_request,
        )

        await mirror_leave_request(created)
    return created


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
async def sync_employmenthero_leave(mock: bool = False, since: str | None = None) -> dict:
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
                    }
                ],
                "leave_balances": [
                    {
                        "employee_id": "emp-mock-1",
                        "category_id": "lc-1",
                        "balance_key": "emp-mock-1:lc-1",
                        "balance_hours": 168.0,
                        "accrued_hours": 224.0,
                    }
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
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "leave",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        org_path,
        _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
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
