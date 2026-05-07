"""Webex rooms (spaces) and memberships — live reads + sync snapshot."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_webex_rooms(
    room_type: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List Webex rooms (spaces) the active token can see.

    ``room_type`` is one of ``"direct"`` (1:1) or ``"group"``.
    """
    if mock:
        rows = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0x",
                "title": "Battersea Portfolio — Ops",
                "type": "group",
                "isLocked": False,
                "lastActivity": "2026-05-04T15:00:00.000Z",
                "created": "2025-09-15T09:00:00.000Z",
                "creatorId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
            },
            {
                "id": "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0y",
                "title": "Maintenance Crew",
                "type": "group",
                "isLocked": False,
                "lastActivity": "2026-05-05T08:00:00.000Z",
                "created": "2025-09-20T09:00:00.000Z",
                "creatorId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
            },
        ]
        return {"rooms": rows, "count": len(rows)}

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    params: dict = {"max": min(limit, 100)}
    if room_type:
        params["type"] = room_type
    body = await webex_get("/v1/rooms", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"rooms": items, "count": len(items)}


@custom_function()
async def get_webex_room(room_id: str, mock: bool = True) -> dict:
    """Get one room by id."""
    if mock:
        return {
            "id": str(room_id),
            "title": "Battersea Portfolio — Ops",
            "type": "group",
            "isLocked": False,
            "lastActivity": "2026-05-04T15:00:00.000Z",
            "created": "2025-09-15T09:00:00.000Z",
            "creatorId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    return await webex_get(f"/v1/rooms/{room_id}")


@custom_function()
async def list_webex_room_memberships(
    room_id: str | None = None,
    person_email: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List memberships, scoped by ``room_id`` or ``person_email``.

    Webex requires at least one of those filters for non-admin tokens.
    """
    if mock:
        rows = [
            {
                "id": "memb-mock-1",
                "roomId": room_id or "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0x",
                "personId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                "personEmail": "alex@example.test",
                "personDisplayName": "Alex Example",
                "isModerator": True,
                "created": "2025-09-15T09:00:00.000Z",
            },
            {
                "id": "memb-mock-2",
                "roomId": room_id or "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0x",
                "personId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTI",
                "personEmail": "sam@example.test",
                "personDisplayName": "Sam Sample",
                "isModerator": False,
                "created": "2025-09-15T09:00:00.000Z",
            },
        ]
        return {"memberships": rows, "count": len(rows)}

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    params: dict = {"max": min(limit, 100)}
    if room_id:
        params["roomId"] = room_id
    if person_email:
        params["personEmail"] = person_email
    body = await webex_get("/v1/memberships", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"memberships": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
async def sync_webex_rooms(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot rooms (group type only — direct/1:1 rooms excluded for
    privacy and volume reasons) plus their memberships into the
    canonical envelope.
    """
    import datetime as _dt

    schema_version = "webex.rooms.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        rooms = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0x",
                "title": "Battersea Portfolio — Ops",
                "type": "group",
                "is_locked": False,
                "last_activity": "2026-05-04T15:00:00.000Z",
                "created_at": "2025-09-15T09:00:00.000Z",
                "creator_id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"rooms": rooms, "room_memberships": []},
            "metadata": {
                "integration": "webex",
                "object_type": "rooms",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    body = await webex_get("/v1/rooms", params={"type": "group", "max": 100})
    if "error" in body:
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    items = body.get("items") or []
    rooms: list[dict] = []
    memberships: list[dict] = []
    for r in items:
        rooms.append(
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "type": r.get("type"),
                "is_locked": r.get("isLocked"),
                "last_activity": r.get("lastActivity"),
                "created_at": r.get("created"),
                "creator_id": r.get("creatorId"),
            }
        )
        memb_body = await webex_get(
            "/v1/memberships",
            params={"roomId": r.get("id"), "max": 100},
        )
        if "error" not in memb_body:
            for m in memb_body.get("items") or []:
                memberships.append(
                    {
                        "id": m.get("id"),
                        "room_id": m.get("roomId"),
                        "person_id": m.get("personId"),
                        "person_email": m.get("personEmail"),
                        "person_display_name": m.get("personDisplayName"),
                        "is_moderator": m.get("isModerator"),
                        "created_at": m.get("created"),
                    }
                )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"rooms": rooms, "room_memberships": memberships},
        "metadata": {
            "integration": "webex",
            "object_type": "rooms",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "rooms": len(rooms),
                "room_memberships": len(memberships),
            },
        },
    }
