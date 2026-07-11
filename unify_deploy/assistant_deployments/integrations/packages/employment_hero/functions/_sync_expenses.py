"""Employment Hero expenses sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_expenses(
    mock: bool = False,
    since: str | None = None,
) -> dict:
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
                    },
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
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "expenses",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        org_path,
        _org_id_or_error,
    )
    from unify_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
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
