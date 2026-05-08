"""Webex meetings — live reads + invitees + sync snapshot.

Meetings are the core of Webex integration value: each carries an
``id``, ``title``, ``start``/``end``, host, and a list of invitees by
email — the email is the join key into HubSpot contacts and Employment
Hero employees.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_webex_meetings(
    from_iso: str | None = None,
    to_iso: str | None = None,
    state: str | None = None,
    host_email: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List Webex meetings.

    ``state`` is one of ``active``, ``scheduled``, ``ready``, ``lobby``,
    ``inProgress``, ``ended``, ``missed``, ``expired``.  Past meetings
    show as ``ended``.

    Defaults to "the last 90 days" when neither ``from_iso`` nor
    ``to_iso`` is provided.
    """
    if mock:
        rows = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                "title": "Battersea — Q2 ops review",
                "start": "2026-05-06T10:00:00Z",
                "end": "2026-05-06T11:00:00Z",
                "hostEmail": "alex@example.test",
                "state": "scheduled",
                "siteUrl": "example.webex.com",
                "webLink": "https://example.webex.com/...",
            },
            {
                "id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                "title": "Maintenance crew weekly",
                "start": "2026-05-04T08:00:00Z",
                "end": "2026-05-04T08:30:00Z",
                "hostEmail": "alex@example.test",
                "state": "ended",
                "siteUrl": "example.webex.com",
                "webLink": "https://example.webex.com/...",
            },
        ]
        return {"meetings": rows, "count": len(rows)}

    import datetime as _dt
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
        get_webex_config,
    )

    cfg = get_webex_config()
    params: dict = {"max": min(limit, 100)}
    if from_iso:
        params["from"] = from_iso
    else:
        lookback = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(
            days=cfg["meeting_lookback_days"]
        )
        # Webex rejects ``+00:00``; needs the ``Z`` UTC suffix.
        params["from"] = lookback.strftime("%Y-%m-%dT%H:%M:%SZ")
    if to_iso:
        params["to"] = to_iso
    if state:
        params["state"] = state
    if host_email:
        params["hostEmail"] = host_email

    body = await webex_get("/v1/meetings", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"meetings": items, "count": len(items)}


@custom_function()
async def get_webex_meeting(meeting_id: str, mock: bool = True) -> dict:
    """Get one meeting by id."""
    if mock:
        return {
            "id": str(meeting_id),
            "title": "Battersea — Q2 ops review",
            "start": "2026-05-06T10:00:00Z",
            "end": "2026-05-06T11:00:00Z",
            "hostEmail": "alex@example.test",
            "state": "scheduled",
            "siteUrl": "example.webex.com",
            "webLink": "https://example.webex.com/...",
            "agenda": "Q2 review of Battersea portfolio occupancy + maintenance backlog.",
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    return await webex_get(f"/v1/meetings/{meeting_id}")


@custom_function()
async def list_webex_meeting_invitees(
    meeting_id: str,
    mock: bool = True,
) -> dict:
    """List invitees for one meeting.  Email is the join key for
    cross-app linking (HubSpot contacts, EH employees)."""
    if mock:
        rows = [
            {
                "id": "inv-mock-1",
                "meetingId": meeting_id,
                "email": "alex@example.test",
                "displayName": "Alex Example",
                "coHost": True,
                "panelist": False,
            },
            {
                "id": "inv-mock-2",
                "meetingId": meeting_id,
                "email": "sam@example.test",
                "displayName": "Sam Sample",
                "coHost": False,
                "panelist": False,
            },
        ]
        return {"invitees": rows, "count": len(rows)}

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    body = await webex_get(
        "/v1/meetingInvitees",
        params={"meetingId": meeting_id, "max": 100},
    )
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"invitees": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
async def sync_webex_meetings(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot meetings + invitees into the canonical envelope.

    ``since`` is an ISO 8601 timestamp; when set, only meetings starting
    on or after that watermark are pulled.  Otherwise falls back to the
    configured lookback window.
    """
    import datetime as _dt

    schema_version = "webex.meetings.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        meetings = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                "title": "Battersea — Q2 ops review",
                "start": "2026-05-06T10:00:00Z",
                "end": "2026-05-06T11:00:00Z",
                "host_email": "alex@example.test",
                "state": "scheduled",
                "site_url": "example.webex.com",
                "web_link": "https://example.webex.com/...",
                "updated_at": started,
            },
        ]
        invitees = [
            {
                "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                "email": "alex@example.test",
                "display_name": "Alex Example",
                "co_host": True,
                "panelist": False,
            },
            {
                "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                "email": "sam@example.test",
                "display_name": "Sam Sample",
                "co_host": False,
                "panelist": False,
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"meetings": meetings, "meeting_invitees": invitees},
            "metadata": {
                "integration": "webex",
                "object_type": "meetings",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
        get_webex_config,
    )

    cfg = get_webex_config()
    params: dict = {"max": cfg["api_page_size"]}
    if since:
        params["from"] = since
    else:
        lookback = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(
            days=cfg["meeting_lookback_days"]
        )
        # Webex rejects ``+00:00``; needs the ``Z`` UTC suffix.
        params["from"] = lookback.strftime("%Y-%m-%dT%H:%M:%SZ")

    body = await webex_get("/v1/meetings", params=params)
    if "error" in body:
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    items = body.get("items") or []
    meetings: list[dict] = []
    invitees: list[dict] = []
    for m in items:
        meetings.append(
            {
                "id": m.get("id"),
                "title": m.get("title"),
                "start": m.get("start"),
                "end": m.get("end"),
                "host_email": m.get("hostEmail"),
                "host_display_name": m.get("hostDisplayName"),
                "host_user_id": m.get("hostUserId"),
                "state": m.get("state"),
                "timezone": m.get("timezone"),
                "site_url": m.get("siteUrl"),
                "web_link": m.get("webLink"),
                "agenda": m.get("agenda"),
                "meeting_type": m.get("meetingType"),
                "scheduled_type": m.get("scheduledType"),
                "is_recurring": m.get("isRecurring"),
                "updated_at": started,
            }
        )
        inv_body = await webex_get(
            "/v1/meetingInvitees",
            params={"meetingId": m.get("id"), "max": 100},
        )
        if "error" not in inv_body:
            for inv in inv_body.get("items") or []:
                email = inv.get("email") or ""
                invitees.append(
                    {
                        "meeting_id": m.get("id"),
                        "email": email.lower() if email else None,
                        "display_name": inv.get("displayName"),
                        "co_host": inv.get("coHost"),
                        "panelist": inv.get("panelist"),
                    }
                )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"meetings": meetings, "meeting_invitees": invitees},
        "metadata": {
            "integration": "webex",
            "object_type": "meetings",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "meetings": len(meetings),
                "meeting_invitees": len(invitees),
            },
        },
    }
