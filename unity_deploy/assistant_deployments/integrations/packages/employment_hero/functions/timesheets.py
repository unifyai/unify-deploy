"""Employment Hero timesheets: read, submit, update, snapshot."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employmenthero_timesheets(
    employee_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List timesheet entries with optional filters."""
    if mock:
        return {
            "entries": [
                {
                    "id": "ts-mock-1",
                    "employee_id": "emp-mock-2",
                    "date": "2026-04-29",
                    "start_time": "08:00",
                    "end_time": "17:00",
                    "hours": 8.5,
                    "project": "Battersea Portfolio",
                    "status": "submitted",
                    "notes": "Routine maintenance",
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
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    if status:
        params["status"] = status
    body = await eh_get(org_path("/timesheet_entries"), params=params)
    if "error" in body:
        return body
    return {"entries": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_timesheet_entry(entry_id: str, mock: bool = True) -> dict:
    """Get one timesheet entry by id."""
    if mock:
        return {
            "id": str(entry_id),
            "employee_id": "emp-mock-2",
            "date": "2026-04-29",
            "hours": 8.5,
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
    body = await eh_get(org_path(f"/timesheet_entries/{entry_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def submit_employmenthero_timesheet_entry(
    employee_id: str,
    date: str,
    hours: float,
    start_time: str | None = None,
    end_time: str | None = None,
    project: str | None = None,
    notes: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Submit a new timesheet entry.

    HIGH-STAKES WRITE — gated by ``confirm=True`` plus
    ``EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true``.
    """
    if mock:
        return {
            "id": "ts-mock-new",
            "employee_id": employee_id,
            "date": date,
            "hours": hours,
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
    payload: dict = {"employee_id": employee_id, "date": date, "hours": hours}
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if project:
        payload["project"] = project
    if notes:
        payload["notes"] = notes
    body = await eh_post(org_path("/timesheet_entries"), payload)
    return body


@custom_function()
async def update_employmenthero_timesheet_entry(
    entry_id: str,
    hours: float | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    project: str | None = None,
    notes: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Update an existing timesheet entry."""
    if mock:
        return {"id": str(entry_id), "hours": hours, "_mocked": True}

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_patch,
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
    payload: dict = {}
    if hours is not None:
        payload["hours"] = hours
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if project:
        payload["project"] = project
    if notes:
        payload["notes"] = notes
    return await eh_patch(org_path(f"/timesheet_entries/{entry_id}"), payload)


@custom_function()
async def sync_employmenthero_timesheets(
    mock: bool = False, since: str | None = None
) -> dict:
    """Snapshot timesheet entries (incremental by ``since`` watermark)."""
    import datetime as _dt

    schema_version = "employment-hero.timesheets.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "timesheets": [
                    {
                        "id": "ts-mock-1",
                        "employee_id": "emp-mock-2",
                        "date": "2026-04-29",
                        "start_time": "08:00",
                        "end_time": "17:00",
                        "hours": 8.5,
                        "project": "Battersea Portfolio",
                        "status": "submitted",
                        "updated_at": started,
                    },
                ]
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "timesheets",
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

    params: dict = {}
    if since:
        params["updated_since"] = since
    else:
        # First-run window: pull last 14 days only.  Backfill more by
        # passing full=true to the orchestrator.
        cutoff = _dt.date.today() - _dt.timedelta(days=14)
        params["from_date"] = cutoff.isoformat()

    raw = await eh_paginate(
        org_path("/timesheet_entries"),
        params=params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    rows = [
        {
            "id": r.get("id"),
            "employee_id": r.get("employee_id"),
            "date": r.get("date"),
            "start_time": r.get("start_time"),
            "end_time": r.get("end_time"),
            "hours": r.get("hours"),
            "project": r.get("project"),
            "location_id": r.get("location_id"),
            "status": r.get("status"),
            "approved_by": r.get("approved_by"),
            "approved_at": r.get("approved_at"),
            "notes": r.get("notes"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        for r in raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"timesheets": rows},
        "metadata": {
            "integration": "employment_hero",
            "object_type": "timesheets",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"timesheets": len(rows)},
        },
    }
