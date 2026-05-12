"""Webex rooms sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_webex_sync_tick`` orchestrator via importlib; not
exposed as a registered tool.
"""

from __future__ import annotations


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
