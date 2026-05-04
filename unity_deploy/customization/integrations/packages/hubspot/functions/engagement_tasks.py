"""HubSpot Engagements - Tasks (create / update / complete / sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "hs_task_subject", "hs_task_body", "hs_task_status", "hs_task_priority",
    "hs_task_type", "hs_timestamp", "hubspot_owner_id",
    "hs_object_id", "hs_lastmodifieddate",
]

_MOCK_TASK = {
    "id": "T1001",
    "properties": {
        "hs_task_subject": "Follow up with prospective tenant",
        "hs_task_body": "Send unit availability for August move-in.",
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": "MEDIUM",
        "hs_task_type": "TODO",
        "hs_timestamp": "2026-04-28T09:00:00Z",
        "hubspot_owner_id": "60001",
        "hs_object_id": "T1001",
        "hs_lastmodifieddate": "2026-04-26T16:00:00Z",
    },
    "createdAt": "2026-04-26T16:00:00Z",
    "updatedAt": "2026-04-26T16:00:00Z",
    "archived": False,
}


@custom_function()
async def create_task(
    subject: str,
    body: str = "",
    due_iso: str | None = None,
    priority: str = "MEDIUM",
    task_type: str = "TODO",
    owner_id: str | None = None,
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Create a HubSpot task."""
    if mock:
        return {**_MOCK_TASK, "id": "T99001",
                "properties": {**_MOCK_TASK["properties"],
                               "hs_task_subject": subject,
                               "hs_task_body": body,
                               "hs_task_priority": priority,
                               "hs_task_type": task_type,
                               "hs_timestamp": due_iso or _now_ms_str()}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    properties = {
        "hs_task_subject": subject,
        "hs_task_body": body,
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": priority,
        "hs_task_type": task_type,
        "hs_timestamp": due_iso or _now_ms_str(),
    }
    if owner_id:
        properties["hubspot_owner_id"] = str(owner_id)

    body_payload: dict = {"properties": properties}
    if associations:
        body_payload["associations"] = _build_associations(
            associations, {"contact": 204, "company": 192, "deal": 216, "ticket": 230},
        )
    return await hubspot_post("/crm/v3/objects/tasks", body_payload)


@custom_function()
async def update_task(task_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch a task's properties (e.g. due date, priority)."""
    if mock:
        return {**_MOCK_TASK, "id": str(task_id),
                "properties": {**_MOCK_TASK["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/tasks/{task_id}",
        {"properties": properties},
    )


@custom_function()
async def complete_task(task_id: str, mock: bool = True) -> dict:
    """Mark a task as completed."""
    if mock:
        return {**_MOCK_TASK, "id": str(task_id),
                "properties": {**_MOCK_TASK["properties"], "hs_task_status": "COMPLETED"}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/tasks/{task_id}",
        {"properties": {"hs_task_status": "COMPLETED"}},
    )


@custom_function()
async def list_tasks(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_TASK, "id": f"T{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/tasks", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_tasks(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.tasks.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )
        rows = [normalize_engagement({**_MOCK_TASK, "id": f"T{1000 + i}"},
                                     engagement_type="task") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"tasks": rows},
            "metadata": {"object_type": "tasks", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_engagement,
    )

    cfg = get_hubspot_config()
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "tasks",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"tasks": rows},
                    "metadata": {"object_type": "tasks", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_engagement(r, engagement_type="task")
                    for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"tasks": rows},
        "metadata": {"object_type": "tasks", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }


def _build_associations(refs: list[dict], type_ids: dict[str, int]) -> list[dict]:
    out = []
    for ref in refs:
        obj = ref.get("object_type", "").rstrip("s")
        type_id = type_ids.get(obj)
        if type_id is None:
            continue
        out.append({
            "to": {"id": str(ref["id"])},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": type_id}],
        })
    return out


def _now_ms_str() -> str:
    import datetime as _dt
    return str(int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000))
