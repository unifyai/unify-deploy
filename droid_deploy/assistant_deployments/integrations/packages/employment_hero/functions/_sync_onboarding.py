"""Employment Hero onboarding sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_onboarding(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    import datetime as _dt

    schema_version = "employment-hero.onboarding.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "onboardings": [
                    {
                        "id": "ob-1",
                        "name": "Maintenance Operative Onboarding",
                        "version": "2",
                        "is_active": True,
                        "updated_at": started,
                    },
                ],
                "onboarding_tasks": [
                    {
                        "id": "obt-1",
                        "onboarding_id": "ob-1",
                        "name": "Right to Work check",
                        "is_required": True,
                        "order": 1,
                        "updated_at": started,
                    },
                ],
                "employee_onboarding_status": [
                    {
                        "id": "eos-1",
                        "employee_id": "emp-mock-2",
                        "onboarding_id": "ob-1",
                        "task_id": "obt-1",
                        "status": "completed",
                        "completed_at": "2024-02-13T09:00:00Z",
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "onboarding",
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

    obs_raw = await eh_paginate(
        org_path("/onboardings"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    onboardings = [
        {
            "id": o.get("id"),
            "name": o.get("name"),
            "version": o.get("version"),
            "is_active": o.get("is_active"),
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
        }
        for o in obs_raw
    ]

    tasks_raw = await eh_paginate(
        org_path("/onboarding_tasks"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    onboarding_tasks = [
        {
            "id": t.get("id"),
            "onboarding_id": t.get("onboarding_id"),
            "name": t.get("name"),
            "category": t.get("category"),
            "is_required": t.get("is_required"),
            "order": t.get("order"),
            "updated_at": t.get("updated_at"),
        }
        for t in tasks_raw
    ]

    status_params: dict = {}
    if since:
        status_params["updated_since"] = since
    status_raw = await eh_paginate(
        org_path("/employee_onboarding"),
        params=status_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    statuses = [
        {
            "id": s.get("id"),
            "employee_id": s.get("employee_id"),
            "onboarding_id": s.get("onboarding_id"),
            "task_id": s.get("task_id"),
            "status": s.get("status"),
            "completed_at": s.get("completed_at"),
            "updated_at": s.get("updated_at"),
        }
        for s in status_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "onboardings": onboardings,
            "onboarding_tasks": onboarding_tasks,
            "employee_onboarding_status": statuses,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "onboarding",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "onboardings": len(onboardings),
                "onboarding_tasks": len(onboarding_tasks),
                "employee_onboarding_status": len(statuses),
            },
        },
    }
