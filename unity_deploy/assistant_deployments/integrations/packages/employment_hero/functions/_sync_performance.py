"""Employment Hero performance sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_performance(
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
