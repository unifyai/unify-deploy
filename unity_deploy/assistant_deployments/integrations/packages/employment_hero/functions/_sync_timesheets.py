"""Employment Hero timesheets sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_timesheets(
    mock: bool = False,
    since: str | None = None,
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
                ],
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
