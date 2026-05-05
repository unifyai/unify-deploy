"""Employment Hero onboarding — processes, tasks, per-employee status."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_onboardings(mock: bool = True) -> dict:
    if mock:
        return {"onboardings": [
            {"id": "ob-1", "name": "Maintenance Operative Onboarding",
             "version": "2", "is_active": True},
            {"id": "ob-2", "name": "Property Manager Onboarding",
             "version": "1", "is_active": True},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/onboardings"))
    if "error" in body:
        return body
    return {"onboardings": body.get("data") or body.get("items") or []}


@custom_function()
async def get_onboarding(onboarding_id: str, mock: bool = True) -> dict:
    if mock:
        return {"id": str(onboarding_id),
                "name": "Maintenance Operative Onboarding",
                "version": "2", "is_active": True}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/onboardings/{onboarding_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_onboarding_tasks(
    onboarding_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {"tasks": [
            {"id": "obt-1", "onboarding_id": "ob-1",
             "name": "Right to Work check",
             "category": "compliance", "is_required": True, "order": 1},
            {"id": "obt-2", "onboarding_id": "ob-1",
             "name": "Gas Safe certificate upload",
             "category": "qualification", "is_required": True, "order": 2},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    if onboarding_id:
        path = org_path(f"/onboardings/{onboarding_id}/tasks")
    else:
        path = org_path("/onboarding_tasks")
    body = await eh_get(path)
    if "error" in body:
        return body
    return {"tasks": body.get("data") or body.get("items") or []}


@custom_function()
async def list_employee_onboarding_status(
    employee_id: str | None = None,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    """Per-employee onboarding completion status."""
    if mock:
        return {"statuses": [
            {"employee_id": "emp-mock-2", "onboarding_id": "ob-1",
             "task_id": "obt-1", "status": "completed",
             "completed_at": "2024-02-13T09:00:00Z"},
            {"employee_id": "emp-mock-2", "onboarding_id": "ob-1",
             "task_id": "obt-2", "status": "outstanding",
             "completed_at": None},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    if employee_id:
        path = org_path(f"/employees/{employee_id}/onboarding")
    else:
        path = org_path("/employee_onboarding")
    params: dict = {}
    if status:
        params["status"] = status
    body = await eh_get(path, params=params)
    if "error" in body:
        return body
    return {"statuses": body.get("data") or body.get("items") or []}


@custom_function()
async def sync_onboarding(mock: bool = False, since: str | None = None) -> dict:
    import datetime as _dt
    schema_version = "employment-hero.onboarding.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "onboardings": [{"id": "ob-1",
                                  "name": "Maintenance Operative Onboarding",
                                  "version": "2", "is_active": True,
                                  "updated_at": started}],
                "onboarding_tasks": [{"id": "obt-1", "onboarding_id": "ob-1",
                                       "name": "Right to Work check",
                                       "is_required": True, "order": 1,
                                       "updated_at": started}],
                "employee_onboarding_status": [
                    {"id": "eos-1", "employee_id": "emp-mock-2",
                     "onboarding_id": "ob-1", "task_id": "obt-1",
                     "status": "completed",
                     "completed_at": "2024-02-13T09:00:00Z",
                     "updated_at": started},
                ],
            },
            "metadata": {"integration": "employment_hero",
                         "object_type": "onboarding",
                         "started_at": started, "mode": "mock"},
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate, org_path, _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
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
    onboardings = [{
        "id": o.get("id"), "name": o.get("name"),
        "version": o.get("version"), "is_active": o.get("is_active"),
        "created_at": o.get("created_at"), "updated_at": o.get("updated_at"),
    } for o in obs_raw]

    tasks_raw = await eh_paginate(
        org_path("/onboarding_tasks"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    onboarding_tasks = [{
        "id": t.get("id"), "onboarding_id": t.get("onboarding_id"),
        "name": t.get("name"), "category": t.get("category"),
        "is_required": t.get("is_required"), "order": t.get("order"),
        "updated_at": t.get("updated_at"),
    } for t in tasks_raw]

    status_params: dict = {}
    if since:
        status_params["updated_since"] = since
    status_raw = await eh_paginate(
        org_path("/employee_onboarding"),
        params=status_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    statuses = [{
        "id": s.get("id"),
        "employee_id": s.get("employee_id"),
        "onboarding_id": s.get("onboarding_id"),
        "task_id": s.get("task_id"),
        "status": s.get("status"),
        "completed_at": s.get("completed_at"),
        "updated_at": s.get("updated_at"),
    } for s in status_raw]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "onboardings": onboardings,
            "onboarding_tasks": onboarding_tasks,
            "employee_onboarding_status": statuses,
        },
        "metadata": {
            "integration": "employment_hero", "object_type": "onboarding",
            "organisation_id": org_id, "started_at": started,
            "finished_at": finished, "since": since,
            "row_counts": {
                "onboardings": len(onboardings),
                "onboarding_tasks": len(onboarding_tasks),
                "employee_onboarding_status": len(statuses),
            },
        },
    }
