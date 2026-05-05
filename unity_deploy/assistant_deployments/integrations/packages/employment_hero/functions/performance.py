"""Employment Hero performance management — reviews, goals, 1:1s, feedback.

HIGH-TIER capability.  Snapshot strips free-text fields; live reads
return full content but the assistant must follow refusal patterns in
``guidance/sensitive_data.md``.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function

# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@custom_function()
async def list_reviews(
    employee_id: str | None = None,
    period: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "reviews": [
                {
                    "id": "rev-1",
                    "employee_id": "emp-mock-1",
                    "reviewer_id": "emp-mock-3",
                    "period": "2026Q1",
                    "review_period_start": "2026-01-01",
                    "review_period_end": "2026-03-31",
                    "status": "completed",
                    "rating": 4,
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
    if employee_id:
        params["employee_id"] = employee_id
    if period:
        params["period"] = period
    body = await eh_get(org_path("/reviews"), params=params)
    if "error" in body:
        return body
    return {"reviews": body.get("data") or body.get("items") or []}


@custom_function()
async def get_review(review_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(review_id),
            "employee_id": "emp-mock-1",
            "reviewer_id": "emp-mock-3",
            "period": "2026Q1",
            "rating": 4,
            "self_assessment": "Hit Q1 occupancy target by mid-March.",
            "manager_feedback": "Strong delivery; develop reporting depth.",
            "improvement_areas": "Stakeholder communication during EICR remediation.",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/reviews/{review_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@custom_function()
async def list_goals(
    employee_id: str | None = None,
    status: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "goals": [
                {
                    "id": "goal-1",
                    "employee_id": "emp-mock-1",
                    "title": "Q2 occupancy ≥ 92% across Battersea Portfolio",
                    "target_value": 92.0,
                    "current_value": 88.5,
                    "unit": "percent",
                    "status": "in_progress",
                    "due_date": "2026-06-30",
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
    if employee_id:
        params["employee_id"] = employee_id
    if status:
        params["status"] = status
    body = await eh_get(org_path("/goals"), params=params)
    if "error" in body:
        return body
    return {"goals": body.get("data") or body.get("items") or []}


@custom_function()
async def get_goal(goal_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(goal_id),
            "title": "Q2 occupancy ≥ 92%",
            "target_value": 92.0,
            "current_value": 88.5,
            "unit": "percent",
            "status": "in_progress",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/goals/{goal_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def create_goal(
    employee_id: str,
    title: str,
    target_value: float | None = None,
    unit: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Create a new goal.  HIGH-STAKES WRITE."""
    if mock:
        return {
            "id": "goal-mock-new",
            "employee_id": employee_id,
            "title": title,
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
    payload: dict = {"employee_id": employee_id, "title": title}
    for k, v in (
        ("target_value", target_value),
        ("unit", unit),
        ("due_date", due_date),
        ("description", description),
    ):
        if v is not None:
            payload[k] = v
    return await eh_post(org_path("/goals"), payload)


@custom_function()
async def update_goal_progress(
    goal_id: str,
    current_value: float,
    note: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Update progress on an existing goal.  HIGH-STAKES WRITE."""
    if mock:
        return {"id": str(goal_id), "current_value": current_value, "_mocked": True}
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
    payload: dict = {"current_value": current_value}
    if note:
        payload["progress_note"] = note
    return await eh_patch(org_path(f"/goals/{goal_id}"), payload)


# ---------------------------------------------------------------------------
# 1:1s
# ---------------------------------------------------------------------------


@custom_function()
async def list_one_on_ones(
    employee_id: str | None = None,
    manager_id: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "one_on_ones": [
                {
                    "id": "111-1",
                    "employee_id": "emp-mock-1",
                    "manager_id": "emp-mock-3",
                    "scheduled_at": "2026-04-30T15:00:00Z",
                    "duration_minutes": 30,
                    "status": "completed",
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
    params: dict = {"limit": min(limit, 100)}
    if employee_id:
        params["employee_id"] = employee_id
    if manager_id:
        params["manager_id"] = manager_id
    body = await eh_get(org_path("/one_on_ones"), params=params)
    if "error" in body:
        return body
    return {"one_on_ones": body.get("data") or body.get("items") or []}


@custom_function()
async def get_one_on_one(one_on_one_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(one_on_one_id),
            "employee_id": "emp-mock-1",
            "manager_id": "emp-mock-3",
            "scheduled_at": "2026-04-30T15:00:00Z",
            "agenda_notes": "Review March occupancy variance.",
            "discussion_notes": "Identified two longer-than-usual voids.",
            "action_items": "Stage chase + escalate via Service Connect.",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/one_on_ones/{one_on_one_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def create_one_on_one(
    employee_id: str,
    manager_id: str,
    scheduled_at: str,
    duration_minutes: int = 30,
    agenda_notes: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Schedule a 1:1.  HIGH-STAKES WRITE."""
    if mock:
        return {
            "id": "111-mock-new",
            "employee_id": employee_id,
            "manager_id": manager_id,
            "scheduled_at": scheduled_at,
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
    payload: dict = {
        "employee_id": employee_id,
        "manager_id": manager_id,
        "scheduled_at": scheduled_at,
        "duration_minutes": duration_minutes,
    }
    if agenda_notes:
        payload["agenda_notes"] = agenda_notes
    return await eh_post(org_path("/one_on_ones"), payload)


@custom_function()
async def update_one_on_one_notes(
    one_on_one_id: str,
    discussion_notes: str | None = None,
    action_items: str | None = None,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Update 1:1 notes.  HIGH-STAKES WRITE."""
    if mock:
        return {"id": str(one_on_one_id), "_mocked": True}
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
    if discussion_notes is not None:
        payload["discussion_notes"] = discussion_notes
    if action_items is not None:
        payload["action_items"] = action_items
    return await eh_patch(org_path(f"/one_on_ones/{one_on_one_id}"), payload)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


@custom_function()
async def list_feedback(
    employee_id: str | None = None,
    feedback_type: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "feedback": [
                {
                    "id": "fb-1",
                    "subject_employee_id": "emp-mock-1",
                    "author_id": "emp-mock-3",
                    "type": "appreciation",
                    "created_at": "2026-04-12T10:00:00Z",
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
    if employee_id:
        params["employee_id"] = employee_id
    if feedback_type:
        params["type"] = feedback_type
    body = await eh_get(org_path("/feedback"), params=params)
    if "error" in body:
        return body
    return {"feedback": body.get("data") or body.get("items") or []}


@custom_function()
async def list_peer_feedback(
    employee_id: str,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "peer_feedback": [
                {
                    "id": "pf-1",
                    "subject_employee_id": employee_id,
                    "author_id": "emp-mock-2",
                    "type": "peer_review",
                    "created_at": "2026-03-20T14:00:00Z",
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
    body = await eh_get(org_path(f"/employees/{employee_id}/peer_feedback"))
    if "error" in body:
        return body
    return {"peer_feedback": body.get("data") or body.get("items") or []}


@custom_function()
async def give_feedback(
    subject_employee_id: str,
    feedback_type: str,
    body: str,
    is_anonymous: bool = False,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Give feedback on an employee.  HIGH-STAKES WRITE."""
    if mock:
        return {
            "id": "fb-mock-new",
            "subject_employee_id": subject_employee_id,
            "type": feedback_type,
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
    payload: dict = {
        "subject_employee_id": subject_employee_id,
        "type": feedback_type,
        "body": body,
        "is_anonymous": is_anonymous,
    }
    return await eh_post(org_path("/feedback"), payload)


# ---------------------------------------------------------------------------
# Sync snapshot — free-text redacted
# ---------------------------------------------------------------------------


@custom_function()
async def sync_performance(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot reviews, goals, 1:1s, feedback.  Free-text redacted to
    length+hash so DataManager analytics works without exposure.
    """
    import datetime as _dt
    import hashlib

    schema_version = "employment-hero.performance.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    def _redact(text: str | None) -> dict:
        if not text:
            return {"length": 0, "hash": None}
        return {
            "length": len(text),
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "reviews": [
                    {
                        "id": "rev-1",
                        "employee_id": "emp-mock-1",
                        "reviewer_id": "emp-mock-3",
                        "period": "2026Q1",
                        "review_period_start": "2026-01-01",
                        "review_period_end": "2026-03-31",
                        "status": "completed",
                        "rating": 4,
                        "self_assessment_length": 56,
                        "self_assessment_hash": "aaaa1111",
                        "manager_feedback_length": 72,
                        "manager_feedback_hash": "bbbb2222",
                        "improvement_areas_length": 64,
                        "improvement_areas_hash": "cccc3333",
                        "updated_at": started,
                    }
                ],
                "goals": [
                    {
                        "id": "goal-1",
                        "employee_id": "emp-mock-1",
                        "title": "Q2 occupancy ≥ 92% across Battersea Portfolio",
                        "target_value": 92.0,
                        "current_value": 88.5,
                        "unit": "percent",
                        "status": "in_progress",
                        "due_date": "2026-06-30",
                        "updated_at": started,
                    }
                ],
                "one_on_ones": [
                    {
                        "id": "111-1",
                        "employee_id": "emp-mock-1",
                        "manager_id": "emp-mock-3",
                        "scheduled_at": "2026-04-30T15:00:00Z",
                        "duration_minutes": 30,
                        "status": "completed",
                        "discussion_notes_length": 48,
                        "discussion_notes_hash": "dddd4444",
                        "updated_at": started,
                    }
                ],
                "feedback": [
                    {
                        "id": "fb-1",
                        "subject_employee_id": "emp-mock-1",
                        "author_id": "emp-mock-3",
                        "type": "appreciation",
                        "is_anonymous": False,
                        "body_length": 32,
                        "body_hash": "eeee5555",
                        "created_at": "2026-04-12T10:00:00Z",
                        "updated_at": started,
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "performance",
                "started_at": started,
                "mode": "mock",
                "redacted_fields": [
                    "reviews.self_assessment",
                    "reviews.manager_feedback",
                    "reviews.improvement_areas",
                    "one_on_ones.discussion_notes",
                    "one_on_ones.action_items",
                    "feedback.body",
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
    redact = cfg["redact_performance_free_text"]

    def _free_text(value: str | None, prefix: str) -> dict:
        if not redact:
            return {prefix: value}
        red = _redact(value)
        return {f"{prefix}_length": red["length"], f"{prefix}_hash": red["hash"]}

    rev_params: dict = {}
    if since:
        rev_params["updated_since"] = since
    revs_raw = await eh_paginate(
        org_path("/reviews"),
        params=rev_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    reviews: list[dict] = []
    for r in revs_raw:
        row = {
            "id": r.get("id"),
            "employee_id": r.get("employee_id"),
            "reviewer_id": r.get("reviewer_id"),
            "period": r.get("period"),
            "review_period_start": r.get("review_period_start"),
            "review_period_end": r.get("review_period_end"),
            "status": r.get("status"),
            "rating": r.get("rating"),
            "completed_at": r.get("completed_at"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        row.update(_free_text(r.get("self_assessment"), "self_assessment"))
        row.update(_free_text(r.get("manager_feedback"), "manager_feedback"))
        row.update(_free_text(r.get("improvement_areas"), "improvement_areas"))
        reviews.append(row)

    goal_params: dict = {}
    if since:
        goal_params["updated_since"] = since
    goals_raw = await eh_paginate(
        org_path("/goals"),
        params=goal_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    # Goals: title + values are not redacted (numeric KPIs are operational).
    goals = [
        {
            "id": g.get("id"),
            "employee_id": g.get("employee_id"),
            "title": g.get("title"),
            "description": g.get("description"),
            "target_value": g.get("target_value"),
            "current_value": g.get("current_value"),
            "unit": g.get("unit"),
            "status": g.get("status"),
            "due_date": g.get("due_date"),
            "completed_at": g.get("completed_at"),
            "created_at": g.get("created_at"),
            "updated_at": g.get("updated_at"),
        }
        for g in goals_raw
    ]

    o_params: dict = {}
    if since:
        o_params["updated_since"] = since
    o_raw = await eh_paginate(
        org_path("/one_on_ones"),
        params=o_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    one_on_ones: list[dict] = []
    for o in o_raw:
        row = {
            "id": o.get("id"),
            "employee_id": o.get("employee_id"),
            "manager_id": o.get("manager_id"),
            "scheduled_at": o.get("scheduled_at"),
            "duration_minutes": o.get("duration_minutes"),
            "status": o.get("status"),
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
        }
        row.update(_free_text(o.get("discussion_notes"), "discussion_notes"))
        row.update(_free_text(o.get("action_items"), "action_items"))
        one_on_ones.append(row)

    fb_params: dict = {}
    if since:
        fb_params["updated_since"] = since
    fb_raw = await eh_paginate(
        org_path("/feedback"),
        params=fb_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    feedback: list[dict] = []
    for f in fb_raw:
        row = {
            "id": f.get("id"),
            "subject_employee_id": f.get("subject_employee_id"),
            "author_id": None if f.get("is_anonymous") else f.get("author_id"),
            "type": f.get("type"),
            "is_anonymous": f.get("is_anonymous"),
            "created_at": f.get("created_at"),
            "updated_at": f.get("updated_at"),
        }
        row.update(_free_text(f.get("body"), "body"))
        feedback.append(row)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    redacted_fields = (
        [
            "reviews.self_assessment",
            "reviews.manager_feedback",
            "reviews.improvement_areas",
            "one_on_ones.discussion_notes",
            "one_on_ones.action_items",
            "feedback.body",
        ]
        if redact
        else []
    )

    return {
        "schema_version": schema_version,
        "tables": {
            "reviews": reviews,
            "goals": goals,
            "one_on_ones": one_on_ones,
            "feedback": feedback,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "performance",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "redacted_fields": redacted_fields,
            "row_counts": {
                "reviews": len(reviews),
                "goals": len(goals),
                "one_on_ones": len(one_on_ones),
                "feedback": len(feedback),
            },
        },
    }
