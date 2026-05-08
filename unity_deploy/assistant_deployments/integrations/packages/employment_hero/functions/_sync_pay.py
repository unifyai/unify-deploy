"""Employment Hero pay sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_pay(mock: bool = False, since: str | None = None) -> dict:
    """Snapshot pay run aggregates + banded employment terms.

    Pay runs: org-level totals (gross/tax/net) per period.  No
    per-employee figures.
    Employment terms: rate banded into a coarse range so DataManager
    analytics work without exposing exact salaries.
    """
    import datetime as _dt

    schema_version = "employment-hero.pay.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "pay_runs": [
                    {
                        "id": "pr-1",
                        "period_start": "2026-04-01",
                        "period_end": "2026-04-30",
                        "status": "finalised",
                        "employee_count": 124,
                        "total_gross_pay": 412_500.00,
                        "total_tax": 91_300.00,
                        "total_net_pay": 321_200.00,
                        "currency": "GBP",
                        "finalised_at": "2026-04-29T16:00:00Z",
                        "updated_at": started,
                    }
                ],
                "pay_categories": [
                    {
                        "id": "pc-1",
                        "name": "Ordinary Hours",
                        "is_taxable": True,
                        "is_pensionable": True,
                        "updated_at": started,
                    }
                ],
                "employment_terms": [
                    {
                        "employee_id": "emp-mock-1",
                        "effective_from": "2025-04-01",
                        "effective_to": None,
                        "classification": "full_time",
                        "rate_type": "salary",
                        "annualised_band": "<45,000 GBP",
                        "currency": "GBP",
                        "updated_at": started,
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "pay",
                "started_at": started,
                "mode": "mock",
                "banded_fields": [
                    "employment_terms.annualised_amount -> annualised_band",
                ],
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
    page_size = cfg["api_page_size"]
    max_pages = cfg["max_pages_per_sync"]
    apply_bands = cfg["pay_rate_bands"]

    pr_params: dict = {}
    if since:
        pr_params["updated_since"] = since
    pr_raw = await eh_paginate(
        org_path("/pay_runs"),
        params=pr_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    pay_runs = [
        {
            "id": p.get("id"),
            "period_start": p.get("period_start"),
            "period_end": p.get("period_end"),
            "status": p.get("status"),
            "employee_count": p.get("employee_count"),
            "total_gross_pay": p.get("total_gross_pay"),
            "total_tax": p.get("total_tax"),
            "total_ni": p.get("total_ni") or p.get("total_employee_ni"),
            "total_pension": p.get("total_pension"),
            "total_net_pay": p.get("total_net_pay"),
            "currency": p.get("currency"),
            "finalised_at": p.get("finalised_at"),
            "updated_at": p.get("updated_at"),
        }
        for p in pr_raw
    ]

    pc_raw = await eh_paginate(
        org_path("/pay_categories"),
        page_size=page_size,
        max_pages=max_pages,
    )
    pay_categories = [
        {
            "id": pc.get("id"),
            "name": pc.get("name"),
            "is_taxable": pc.get("is_taxable"),
            "is_pensionable": pc.get("is_pensionable"),
            "updated_at": pc.get("updated_at"),
        }
        for pc in pc_raw
    ]

    et_params: dict = {}
    if since:
        et_params["updated_since"] = since
    et_raw = await eh_paginate(
        org_path("/employment_terms"),
        params=et_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._pay_helpers import (
        band_rate,
    )

    employment_terms: list[dict] = []
    for t in et_raw:
        amount = t.get("annualised_amount") or t.get("amount")
        currency = t.get("currency") or "GBP"
        row = {
            "employee_id": t.get("employee_id"),
            "effective_from": t.get("effective_from"),
            "effective_to": t.get("effective_to"),
            "classification": t.get("classification"),
            "rate_type": t.get("rate_type"),
            "currency": currency,
            "updated_at": t.get("updated_at"),
        }
        if apply_bands:
            row["annualised_band"] = band_rate(amount, currency)
        else:
            row["annualised_amount"] = amount
        employment_terms.append(row)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "pay_runs": pay_runs,
            "pay_categories": pay_categories,
            "employment_terms": employment_terms,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "pay",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "banded_fields": (
                ["employment_terms.annualised_amount -> annualised_band"]
                if apply_bands
                else []
            ),
            "row_counts": {
                "pay_runs": len(pay_runs),
                "pay_categories": len(pay_categories),
                "employment_terms": len(employment_terms),
            },
        },
    }
