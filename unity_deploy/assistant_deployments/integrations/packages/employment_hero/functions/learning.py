"""Employment Hero learning — courses, assignments, completions."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employmenthero_courses(mock: bool = True) -> dict:
    if mock:
        return {
            "courses": [
                {
                    "id": "course-1",
                    "name": "Fair Housing & Equality (UK)",
                    "duration_minutes": 45,
                    "is_mandatory": True,
                    "category": "compliance",
                },
            ]
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/courses"))
    if "error" in body:
        return body
    return {"courses": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_course(course_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(course_id),
            "name": "Fair Housing & Equality (UK)",
            "duration_minutes": 45,
            "is_mandatory": True,
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/courses/{course_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_employmenthero_course_assignments(
    course_id: str | None = None,
    employee_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "assignments": [
                {
                    "id": "ca-1",
                    "course_id": "course-1",
                    "employee_id": "emp-mock-1",
                    "assigned_at": "2026-04-01",
                    "due_date": "2026-04-30",
                    "status": "in_progress",
                },
            ]
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {}
    if course_id:
        params["course_id"] = course_id
    if employee_id:
        params["employee_id"] = employee_id
    body = await eh_get(org_path("/course_assignments"), params=params)
    if "error" in body:
        return body
    return {"assignments": body.get("data") or body.get("items") or []}


@custom_function()
async def list_employmenthero_course_completions(
    course_id: str | None = None,
    employee_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "completions": [
                {
                    "id": "cc-1",
                    "course_id": "course-1",
                    "employee_id": "emp-mock-3",
                    "completed_at": "2026-04-08T11:30:00Z",
                    "score": 92,
                },
            ]
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {}
    if course_id:
        params["course_id"] = course_id
    if employee_id:
        params["employee_id"] = employee_id
    body = await eh_get(org_path("/course_completions"), params=params)
    if "error" in body:
        return body
    return {"completions": body.get("data") or body.get("items") or []}


@custom_function()
async def sync_employmenthero_learning(mock: bool = False, since: str | None = None) -> dict:
    import datetime as _dt

    schema_version = "employment-hero.learning.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "courses": [
                    {
                        "id": "course-1",
                        "name": "Fair Housing & Equality (UK)",
                        "duration_minutes": 45,
                        "is_mandatory": True,
                        "category": "compliance",
                        "updated_at": started,
                    }
                ],
                "course_assignments": [
                    {
                        "id": "ca-1",
                        "course_id": "course-1",
                        "employee_id": "emp-mock-1",
                        "assigned_at": "2026-04-01",
                        "due_date": "2026-04-30",
                        "status": "in_progress",
                        "updated_at": started,
                    }
                ],
                "course_completions": [
                    {
                        "id": "cc-1",
                        "course_id": "course-1",
                        "employee_id": "emp-mock-3",
                        "completed_at": "2026-04-08T11:30:00Z",
                        "score": 92,
                        "updated_at": started,
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "learning",
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

    courses_raw = await eh_paginate(
        org_path("/courses"), page_size=page_size, max_pages=max_pages
    )
    courses = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "description": c.get("description"),
            "duration_minutes": c.get("duration_minutes"),
            "is_mandatory": c.get("is_mandatory"),
            "category": c.get("category"),
            "updated_at": c.get("updated_at"),
        }
        for c in courses_raw
    ]

    a_params: dict = {}
    if since:
        a_params["updated_since"] = since
    assignments_raw = await eh_paginate(
        org_path("/course_assignments"),
        params=a_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    course_assignments = [
        {
            "id": a.get("id"),
            "course_id": a.get("course_id"),
            "employee_id": a.get("employee_id"),
            "assigned_at": a.get("assigned_at"),
            "due_date": a.get("due_date"),
            "status": a.get("status"),
            "updated_at": a.get("updated_at"),
        }
        for a in assignments_raw
    ]

    c_params: dict = {}
    if since:
        c_params["updated_since"] = since
    completions_raw = await eh_paginate(
        org_path("/course_completions"),
        params=c_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    course_completions = [
        {
            "id": c.get("id"),
            "course_id": c.get("course_id"),
            "employee_id": c.get("employee_id"),
            "completed_at": c.get("completed_at"),
            "score": c.get("score"),
            "updated_at": c.get("updated_at"),
        }
        for c in completions_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "courses": courses,
            "course_assignments": course_assignments,
            "course_completions": course_completions,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "learning",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "courses": len(courses),
                "course_assignments": len(course_assignments),
                "course_completions": len(course_completions),
            },
        },
    }
