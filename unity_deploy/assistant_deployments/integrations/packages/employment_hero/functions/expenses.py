"""Employment Hero expenses: claims, categories, snapshot."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_expense_categories(mock: bool = True) -> dict:
    """List expense categories defined for the organisation."""
    if mock:
        return {
            "categories": [
                {
                    "id": "ec-1",
                    "name": "Travel - Mileage",
                    "is_taxable": False,
                    "default_currency": "GBP",
                },
                {
                    "id": "ec-2",
                    "name": "Materials",
                    "is_taxable": True,
                    "default_currency": "GBP",
                },
                {
                    "id": "ec-3",
                    "name": "Subsistence",
                    "is_taxable": False,
                    "default_currency": "GBP",
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
    body = await eh_get(org_path("/expense_categories"))
    if "error" in body:
        return body
    return {"categories": body.get("data") or body.get("items") or []}


@custom_function()
async def list_expense_claims(
    employee_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """List expense claims with optional filters."""
    if mock:
        return {
            "claims": [
                {
                    "id": "ex-mock-1",
                    "employee_id": "emp-mock-2",
                    "category_id": "ec-1",
                    "amount": 12.40,
                    "currency": "GBP",
                    "date": "2026-04-28",
                    "status": "submitted",
                    "description": "Mileage Battersea -> Camden",
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
    body = await eh_get(org_path("/expenses"), params=params)
    if "error" in body:
        return body
    return {"claims": body.get("data") or body.get("items") or []}


@custom_function()
async def get_expense_claim(claim_id: str, mock: bool = True) -> dict:
    """Get one expense claim by id."""
    if mock:
        return {
            "id": str(claim_id),
            "employee_id": "emp-mock-2",
            "amount": 12.40,
            "currency": "GBP",
            "date": "2026-04-28",
            "status": "submitted",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/expenses/{claim_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def submit_expense_claim(
    employee_id: str,
    category_id: str,
    amount: float,
    currency: str,
    date: str,
    description: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Submit a new expense claim.  HIGH-STAKES WRITE."""
    if mock:
        return {
            "id": "ex-mock-new",
            "employee_id": employee_id,
            "amount": amount,
            "currency": currency,
            "status": "submitted",
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
            "error": "Refused: EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES is not true."
        }
    if not confirm:
        return {"error": "Refused: confirm=True is required for live mutations."}
    _, err = _org_id_or_error()
    if err is not None:
        return err
    payload: dict = {
        "employee_id": employee_id,
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "date": date,
    }
    if description:
        payload["description"] = description
    return await eh_post(org_path("/expenses"), payload)


@custom_function()
async def sync_expenses(mock: bool = False, since: str | None = None) -> dict:
    """Snapshot expense claims and categories."""
    import datetime as _dt

    schema_version = "employment-hero.expenses.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "expense_categories": [
                    {
                        "id": "ec-1",
                        "name": "Travel - Mileage",
                        "is_taxable": False,
                        "default_currency": "GBP",
                    }
                ],
                "expense_claims": [
                    {
                        "id": "ex-mock-1",
                        "employee_id": "emp-mock-2",
                        "category_id": "ec-1",
                        "amount": 12.40,
                        "currency": "GBP",
                        "date": "2026-04-28",
                        "status": "submitted",
                        "updated_at": started,
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "expenses",
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
        org_path("/expense_categories"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    expense_categories = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "is_taxable": c.get("is_taxable"),
            "default_currency": c.get("default_currency"),
            "updated_at": c.get("updated_at"),
        }
        for c in cats_raw
    ]

    params: dict = {}
    if since:
        params["updated_since"] = since
    raw = await eh_paginate(
        org_path("/expenses"),
        params=params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    expense_claims = [
        {
            "id": r.get("id"),
            "employee_id": r.get("employee_id"),
            "category_id": r.get("category_id"),
            "amount": r.get("amount"),
            "currency": r.get("currency"),
            "date": r.get("date"),
            "status": r.get("status"),
            "description": r.get("description"),
            "approved_by": r.get("approved_by"),
            "approved_at": r.get("approved_at"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        for r in raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "expense_categories": expense_categories,
            "expense_claims": expense_claims,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "expenses",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "expense_categories": len(expense_categories),
                "expense_claims": len(expense_claims),
            },
        },
    }
