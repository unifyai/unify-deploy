"""Employment Hero surveys sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_surveys(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    import datetime as _dt
    import hashlib

    schema_version = "employment-hero.surveys.snapshot.v1"
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
                "surveys": [
                    {
                        "id": "sv-1",
                        "name": "Q1 2026 Engagement Pulse",
                        "is_anonymous": True,
                        "status": "closed",
                        "opens_at": "2026-03-15",
                        "closes_at": "2026-03-29",
                        "respondent_count": 87,
                        "updated_at": started,
                    },
                ],
                "survey_aggregates": [
                    {
                        "survey_id": "sv-1",
                        "question_id": "q-1",
                        "metric": "mean",
                        "value": 4.1,
                        "n": 87,
                        "updated_at": started,
                    },
                ],
                "survey_responses": [],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "surveys",
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
    force_anon = cfg["surveys_force_anonymous"]

    surveys_raw = await eh_paginate(
        org_path("/surveys"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    surveys: list[dict] = []
    aggregates: list[dict] = []
    responses: list[dict] = []

    for s in surveys_raw:
        sid = s.get("id")
        is_anon = bool(s.get("is_anonymous")) or force_anon
        surveys.append(
            {
                "id": sid,
                "name": s.get("name"),
                "is_anonymous": is_anon,
                "status": s.get("status"),
                "opens_at": s.get("opens_at"),
                "closes_at": s.get("closes_at"),
                "respondent_count": s.get("respondent_count"),
                "questions_json": str(s.get("questions") or []),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
            },
        )

        resp_body = await eh_get(org_path(f"/surveys/{sid}/responses"))
        if "error" in resp_body:
            continue
        items = resp_body.get("data") or resp_body.get("items") or []

        if is_anon:
            # Aggregate per question.
            buckets: dict = {}
            for r in items:
                for ans in r.get("answers") or []:
                    qid = ans.get("question_id")
                    val = ans.get("value")
                    b = buckets.setdefault(qid, {"n": 0, "vals": []})
                    b["n"] += 1
                    if isinstance(val, (int, float)):
                        b["vals"].append(val)
            for qid, b in buckets.items():
                aggregates.append(
                    {
                        "survey_id": sid,
                        "question_id": qid,
                        "metric": "count",
                        "value": b["n"],
                        "n": b["n"],
                        "updated_at": started,
                    },
                )
                if b["vals"]:
                    mean = sum(b["vals"]) / len(b["vals"])
                    aggregates.append(
                        {
                            "survey_id": sid,
                            "question_id": qid,
                            "metric": "mean",
                            "value": mean,
                            "n": len(b["vals"]),
                            "updated_at": started,
                        },
                    )
        else:
            # Attributed responses with redacted free-text.
            for r in items:
                row = {
                    "id": r.get("id"),
                    "survey_id": sid,
                    "respondent_employee_id": r.get("respondent_employee_id"),
                    "submitted_at": r.get("submitted_at"),
                }
                # Per-question answer rows (long format).
                for ans in r.get("answers") or []:
                    qid = ans.get("question_id")
                    val = ans.get("value")
                    if isinstance(val, str):
                        red = _redact(val)
                        responses.append(
                            {
                                **row,
                                "question_id": qid,
                                "answer_text_length": red["length"],
                                "answer_text_hash": red["hash"],
                                "answer_numeric": None,
                            },
                        )
                    elif isinstance(val, (int, float)):
                        responses.append(
                            {
                                **row,
                                "question_id": qid,
                                "answer_text_length": None,
                                "answer_text_hash": None,
                                "answer_numeric": val,
                            },
                        )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "surveys": surveys,
            "survey_aggregates": aggregates,
            "survey_responses": responses,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "surveys",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "surveys": len(surveys),
                "survey_aggregates": len(aggregates),
                "survey_responses": len(responses),
            },
        },
    }
