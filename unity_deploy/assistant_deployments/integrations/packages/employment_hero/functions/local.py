"""Local DataManager query helpers for Employment Hero.

After ``run_employmenthero_sync_tick`` has materialised data into
``EmploymentHero/...`` contexts, these functions are the preferred read
path for analytical queries.  Each returns a ``freshness`` block so the
assistant knows whether to trust the local copy.

Mirrors hubspot/local_query.py.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def query_local_employmenthero_employees(
    name_query: str | None = None,
    team_id: str | None = None,
    location_id: str | None = None,
    status: str = "active",
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Query the synced employees table."""
    if mock:
        return {
            "rows": [
                {
                    "id": "emp-mock-1",
                    "first_name": "Alex",
                    "last_name": "Example",
                    "team_id": "team-mock-1",
                    "location_id": "loc-mock-1",
                    "status": "active",
                },
            ],
            "count": 1,
            "freshness": {
                "last_synced_at": "2026-04-30T02:15:00Z",
                "is_fresh": True,
                "threshold_seconds": 172_800,
            },
        }

    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()

    filters: list[str] = []
    if status:
        filters.append(f"`status` == '{status}'")
    if team_id:
        filters.append(f"`team_id` == '{team_id}'")
    if location_id:
        filters.append(f"`location_id` == '{location_id}'")
    if name_query:
        safe = name_query.replace("'", "''")
        filters.append(f"(`first_name` LIKE '%{safe}%' OR `last_name` LIKE '%{safe}%')")

    rows = await safe_filter(
        dm,
        "EmploymentHero/Employees",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("workforce"),
    }


@custom_function()
async def query_local_employmenthero_teams(
    location_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "team-mock-1",
                    "name": "Property Management",
                    "location_id": "loc-mock-1",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    f = f"`location_id` == '{location_id}'" if location_id else None
    rows = await safe_filter(dm, "EmploymentHero/Teams", filter=f, limit=200)
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("workforce"),
    }


@custom_function()
async def query_local_employmenthero_locations(mock: bool = True) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "loc-mock-1",
                    "name": "Battersea Portfolio",
                    "city": "London",
                    "postcode": "SW11",
                    "country": "GB",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    rows = await safe_filter(dm, "EmploymentHero/Locations", limit=500)
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("workforce"),
    }


@custom_function()
async def query_local_employmenthero_leave_requests(
    employee_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "lr-mock-1",
                    "employee_id": "emp-mock-1",
                    "status": "approved",
                    "start_date": "2026-05-12",
                    "end_date": "2026-05-16",
                    "total_hours": 32.0,
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if status:
        filters.append(f"`status` == '{status}'")
    if from_date:
        filters.append(f"`start_date` >= '{from_date}'")
    if to_date:
        filters.append(f"`end_date` <= '{to_date}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Leave/Requests",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows), "freshness": await freshness("leave")}


@custom_function()
async def query_local_employmenthero_leave_balances(
    employee_id: str | None = None,
    category_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "employee_id": "emp-mock-1",
                    "category_id": "lc-1",
                    "balance_hours": 168.0,
                    "accrued_hours": 224.0,
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if category_id:
        filters.append(f"`category_id` == '{category_id}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Leave/Balances",
        filter=" AND ".join(filters) if filters else None,
        limit=500,
    )
    return {"rows": rows, "count": len(rows), "freshness": await freshness("leave")}


@custom_function()
async def query_local_employmenthero_timesheets(
    employee_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    limit: int = 500,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "ts-mock-1",
                    "employee_id": "emp-mock-2",
                    "date": "2026-04-29",
                    "hours": 8.5,
                    "status": "submitted",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if from_date:
        filters.append(f"`date` >= '{from_date}'")
    if to_date:
        filters.append(f"`date` <= '{to_date}'")
    if status:
        filters.append(f"`status` == '{status}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Timesheets",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("timesheets"),
    }


@custom_function()
async def query_local_employmenthero_expenses(
    employee_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "ex-mock-1",
                    "employee_id": "emp-mock-2",
                    "amount": 12.40,
                    "currency": "GBP",
                    "date": "2026-04-28",
                    "status": "submitted",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if status:
        filters.append(f"`status` == '{status}'")
    if from_date:
        filters.append(f"`date` >= '{from_date}'")
    if to_date:
        filters.append(f"`date` <= '{to_date}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Expenses/Claims",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows), "freshness": await freshness("expenses")}


@custom_function()
async def query_local_employmenthero_qualifications(
    employee_id: str | None = None,
    qualification_id: str | None = None,
    mock: bool = True,
) -> dict:
    """List per-employee qualification records from the synced table."""
    if mock:
        return {
            "rows": [
                {
                    "id": "eq-mock-1",
                    "employee_id": "emp-mock-2",
                    "qualification_id": "qual-mock-1",
                    "qualification_name": "Gas Safe Registration",
                    "expires_at": "2026-06-01",
                    "status": "active",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if qualification_id:
        filters.append(f"`qualification_id` == '{qualification_id}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Qualifications/EmployeeRecords",
        filter=" AND ".join(filters) if filters else None,
        limit=1000,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("qualifications"),
    }


@custom_function()
async def query_local_employmenthero_expiring_qualifications(
    days_ahead: int = 90,
    location_id: str | None = None,
    qualification_id: str | None = None,
    limit: int = 500,
    mock: bool = True,
) -> dict:
    """**Marquee compliance query.**

    Returns operatives with certifications expiring within ``days_ahead``,
    joined to the employee + location dimensions for a read-out the
    assistant can pass straight to a property manager.
    """
    import datetime as _dt

    today = _dt.date.today()
    cutoff = today + _dt.timedelta(days=days_ahead)

    if mock:
        return {
            "rows": [
                {
                    "employee_first_name": "Sam",
                    "employee_last_name": "Sample",
                    "property": "Battersea Portfolio",
                    "certification": "Gas Safe Registration",
                    "expires_at": "2026-06-01",
                    "days_to_expiry": 32,
                },
            ],
            "count": 1,
            "window": {
                "from": today.isoformat(),
                "to": cutoff.isoformat(),
                "days_ahead": days_ahead,
            },
            "freshness": {"is_fresh": True},
        }

    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()

    filters = [
        f"eq.expires_at >= '{today.isoformat()}'",
        f"eq.expires_at <= '{cutoff.isoformat()}'",
        "emp.status == 'active'",
    ]
    if location_id:
        filters.append(f"emp.location_id == '{location_id}'")
    if qualification_id:
        filters.append(f"eq.qualification_id == '{qualification_id}'")

    try:
        rows = await dm.filter_join(
            tables=[
                ("EmploymentHero/Qualifications/EmployeeRecords", "eq"),
                ("EmploymentHero/Qualifications/Catalogue", "q"),
                ("EmploymentHero/Employees", "emp"),
                ("EmploymentHero/Locations", "loc"),
            ],
            join_expr=(
                "eq.qualification_id == q.id "
                "AND eq.employee_id == emp.id "
                "AND emp.location_id == loc.id"
            ),
            filter=" AND ".join(filters),
            select=[
                "emp.first_name AS employee_first_name",
                "emp.last_name AS employee_last_name",
                "loc.name AS property",
                "q.name AS certification",
                "eq.expires_at",
                "eq.days_to_expiry",
                "eq.status",
            ],
            order_by="eq.expires_at",
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error": f"Local join failed: {e!r}",
            "hint": (
                "DataManager filter_join may not be available, or the "
                "underlying tables haven't been synced yet.  Try "
                "list_employee_qualifications(expires_before=...) live "
                "instead, or run probe_tier() to confirm data freshness."
            ),
            "freshness": await freshness("qualifications"),
        }

    return {
        "rows": rows or [],
        "count": len(rows or []),
        "window": {
            "from": today.isoformat(),
            "to": cutoff.isoformat(),
            "days_ahead": days_ahead,
        },
        "freshness": await freshness("qualifications"),
    }


@custom_function()
async def query_local_employmenthero_onboarding_status(
    employee_id: str | None = None,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "employee_id": "emp-mock-2",
                    "task_id": "obt-2",
                    "status": "outstanding",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if status:
        filters.append(f"`status` == '{status}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Onboarding/EmployeeStatus",
        filter=" AND ".join(filters) if filters else None,
        limit=500,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("onboarding"),
    }


@custom_function()
async def query_local_employmenthero_documents(
    employee_id: str | None = None,
    document_type: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "doc-1",
                    "employee_id": "emp-mock-1",
                    "name": "Right To Work — Passport.pdf",
                    "type": "right_to_work",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if document_type:
        filters.append(f"`type` == '{document_type}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Documents",
        filter=" AND ".join(filters) if filters else None,
        limit=500,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("documents"),
    }


@custom_function()
async def query_local_employmenthero_goals(
    employee_id: str | None = None,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "goal-1",
                    "employee_id": "emp-mock-1",
                    "title": "Q2 occupancy ≥ 92%",
                    "current_value": 88.5,
                    "target_value": 92.0,
                    "status": "in_progress",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if status:
        filters.append(f"`status` == '{status}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Performance/Goals",
        filter=" AND ".join(filters) if filters else None,
        limit=200,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("performance"),
    }


@custom_function()
async def query_local_employmenthero_reviews(
    employee_id: str | None = None,
    period: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "rows": [
                {
                    "id": "rev-1",
                    "employee_id": "emp-mock-1",
                    "period": "2026Q1",
                    "rating": 4,
                    "_note": "Free-text fields are redacted in the synced copy.",
                }
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if employee_id:
        filters.append(f"`employee_id` == '{employee_id}'")
    if period:
        filters.append(f"`period` == '{period}'")
    rows = await safe_filter(
        dm,
        "EmploymentHero/Performance/Reviews",
        filter=" AND ".join(filters) if filters else None,
        limit=200,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("performance"),
        "_note": (
            "Free-text fields (self_assessment, manager_feedback, "
            "improvement_areas) are redacted to length+hash in the "
            "synced copy.  Use get_review() live for full content."
        ),
    }
