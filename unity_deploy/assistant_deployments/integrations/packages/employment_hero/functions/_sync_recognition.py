"""Employment Hero recognition sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_recognition(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    import datetime as _dt

    schema_version = "employment-hero.recognition.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "cheers": [
                    {
                        "id": "cheer-1",
                        "from_employee_id": "emp-mock-3",
                        "to_employee_id": "emp-mock-1",
                        "value": "Above and beyond",
                        "created_at": "2026-04-15T10:00:00Z",
                        "updated_at": started,
                    },
                ],
                "hi_fives": [
                    {
                        "id": "hi5-1",
                        "from_employee_id": "emp-mock-2",
                        "to_employee_id": "emp-mock-1",
                        "created_at": "2026-04-20T14:00:00Z",
                        "updated_at": started,
                    },
                ],
                "awards": [
                    {
                        "id": "aw-1",
                        "name": "Property Manager of the Quarter",
                        "recipient_employee_id": "emp-mock-1",
                        "awarded_at": "2026-04-01",
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "recognition",
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
    page_size = cfg["api_page_size"]
    max_pages = cfg["max_pages_per_sync"]

    cparams: dict = {}
    hparams: dict = {}
    if since:
        cparams["updated_since"] = since
        hparams["updated_since"] = since

    cheers_raw = await eh_paginate(
        org_path("/cheers"),
        params=cparams,
        page_size=page_size,
        max_pages=max_pages,
    )
    cheers = [
        {
            "id": c.get("id"),
            "from_employee_id": c.get("from_employee_id"),
            "to_employee_id": c.get("to_employee_id"),
            "value": c.get("value"),
            "message": c.get("message"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        for c in cheers_raw
    ]

    hi5_raw = await eh_paginate(
        org_path("/hi_fives"),
        params=hparams,
        page_size=page_size,
        max_pages=max_pages,
    )
    hi_fives = [
        {
            "id": h.get("id"),
            "from_employee_id": h.get("from_employee_id"),
            "to_employee_id": h.get("to_employee_id"),
            "message": h.get("message"),
            "created_at": h.get("created_at"),
            "updated_at": h.get("updated_at"),
        }
        for h in hi5_raw
    ]

    aw_raw = await eh_paginate(
        org_path("/recognition_awards"),
        page_size=page_size,
        max_pages=max_pages,
    )
    awards = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "description": a.get("description"),
            "recipient_employee_id": a.get("recipient_employee_id"),
            "awarded_at": a.get("awarded_at"),
            "updated_at": a.get("updated_at"),
        }
        for a in aw_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"cheers": cheers, "hi_fives": hi_fives, "awards": awards},
        "metadata": {
            "integration": "employment_hero",
            "object_type": "recognition",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "cheers": len(cheers),
                "hi_fives": len(hi_fives),
                "awards": len(awards),
            },
        },
    }
