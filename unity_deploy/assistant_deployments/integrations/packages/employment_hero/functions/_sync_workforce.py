"""Employment Hero workforce sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_workforce(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot the workforce graph: employees, employments, positions,
    teams, team_memberships, locations.

    Returns the canonical
    ``{schema_version, tables, metadata}`` envelope so the scenario
    runtime can route output to its declared ``data_targets`` contexts.

    ``since`` is an ISO 8601 timestamp; when provided, only employees
    whose ``updated_at`` is on or after the watermark are pulled.
    """
    import datetime as _dt

    schema_version = "employment-hero.workforce.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        employees = [
            {
                "id": "emp-mock-1",
                "first_name": "Alex",
                "last_name": "Example",
                "work_email": "alex@example.test",
                "position": "Property Manager",
                "team_id": "team-mock-1",
                "location_id": "loc-mock-1",
                "employment_type": "full_time",
                "status": "active",
                "start_date": "2023-09-01",
                "country": "GB",
                "updated_at": started,
            },
            {
                "id": "emp-mock-2",
                "first_name": "Sam",
                "last_name": "Sample",
                "work_email": "sam@example.test",
                "position": "Maintenance Operative",
                "team_id": "team-mock-2",
                "location_id": "loc-mock-1",
                "employment_type": "full_time",
                "status": "active",
                "start_date": "2024-02-12",
                "country": "GB",
                "updated_at": started,
            },
        ]
        teams = [
            {
                "id": "team-mock-1",
                "name": "Property Management",
                "manager_id": None,
                "location_id": "loc-mock-1",
                "parent_team_id": None,
                "updated_at": started,
            },
            {
                "id": "team-mock-2",
                "name": "Maintenance",
                "manager_id": None,
                "location_id": "loc-mock-1",
                "parent_team_id": None,
                "updated_at": started,
            },
        ]
        memberships = [
            {"team_id": "team-mock-1", "employee_id": "emp-mock-1", "role": "lead"},
            {"team_id": "team-mock-2", "employee_id": "emp-mock-2", "role": None},
        ]
        locations = [
            {
                "id": "loc-mock-1",
                "name": "Battersea Portfolio",
                "address_line_1": "21 Lavender Hill",
                "city": "London",
                "postcode": "SW11",
                "country": "GB",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {
                "employees": employees,
                "employments": [],
                "positions": [],
                "teams": teams,
                "team_memberships": memberships,
                "locations": locations,
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "workforce",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        eh_get,
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

    emp_params: dict = {"include_terminated": True}
    if since:
        emp_params["updated_since"] = since

    emp_rows_raw = await eh_paginate(
        org_path("/employees"),
        params=emp_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    employees: list[dict] = []
    for e in emp_rows_raw:
        employees.append(
            {
                "id": e.get("id"),
                "first_name": e.get("first_name"),
                "last_name": e.get("last_name"),
                "work_email": e.get("work_email"),
                "personal_email": e.get("personal_email"),
                "phone": e.get("phone"),
                "position": e.get("position"),
                "team_id": e.get("team_id"),
                "manager_id": e.get("manager_id"),
                "employment_type": e.get("employment_type"),
                "start_date": e.get("start_date"),
                "termination_date": e.get("termination_date"),
                "status": e.get("status"),
                "location_id": e.get("location_id"),
                "country": e.get("country"),
                "updated_at": e.get("updated_at"),
            }
        )

    teams_raw = await eh_paginate(
        org_path("/teams"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    teams: list[dict] = []
    memberships: list[dict] = []
    for t in teams_raw:
        teams.append(
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "manager_id": t.get("manager_id"),
                "location_id": t.get("location_id"),
                "parent_team_id": t.get("parent_team_id"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
            }
        )
        # Members per team — best-effort; tier-gated 403 returns empty.
        members_body = await eh_get(org_path(f"/teams/{t.get('id')}/members"))
        if "error" not in members_body:
            members = members_body.get("data") or members_body.get("items") or []
            for m in members:
                memberships.append(
                    {
                        "team_id": t.get("id"),
                        "employee_id": m.get("employee_id") or m.get("id"),
                        "role": m.get("role"),
                    }
                )

    locations_raw = await eh_paginate(
        org_path("/locations"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    locations: list[dict] = [
        {
            "id": loc.get("id"),
            "name": loc.get("name"),
            "address_line_1": loc.get("address_line_1"),
            "address_line_2": loc.get("address_line_2"),
            "city": loc.get("city"),
            "postcode": loc.get("postcode"),
            "country": loc.get("country"),
            "created_at": loc.get("created_at"),
            "updated_at": loc.get("updated_at"),
        }
        for loc in locations_raw
    ]

    # Employments + positions are per-employee endpoints; pull only for
    # employees touched in this delta to keep the sync bounded.
    employments: list[dict] = []
    positions: list[dict] = []
    for e in employees:
        emp_id = e.get("id")
        if not emp_id:
            continue
        emps_body = await eh_get(org_path(f"/employees/{emp_id}/employments"))
        if "error" not in emps_body:
            for empl in emps_body.get("data") or emps_body.get("items") or []:
                employments.append(
                    {
                        "id": empl.get("id"),
                        "employee_id": emp_id,
                        "position": empl.get("position"),
                        "start_date": empl.get("start_date"),
                        "end_date": empl.get("end_date"),
                        "is_current": empl.get("is_current"),
                    }
                )
        pos_body = await eh_get(org_path(f"/employees/{emp_id}/positions"))
        if "error" not in pos_body:
            for p in pos_body.get("data") or pos_body.get("items") or []:
                positions.append(
                    {
                        "id": p.get("id"),
                        "employee_id": emp_id,
                        "title": p.get("title"),
                        "start_date": p.get("start_date"),
                        "end_date": p.get("end_date"),
                        "is_current": p.get("is_current"),
                    }
                )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "employees": employees,
            "employments": employments,
            "positions": positions,
            "teams": teams,
            "team_memberships": memberships,
            "locations": locations,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "workforce",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "employees": len(employees),
                "employments": len(employments),
                "positions": len(positions),
                "teams": len(teams),
                "team_memberships": len(memberships),
                "locations": len(locations),
            },
        },
    }
