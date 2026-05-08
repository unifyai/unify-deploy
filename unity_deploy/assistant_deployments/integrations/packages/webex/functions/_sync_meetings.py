"""Webex meetings sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_webex_sync_tick`` orchestrator via importlib; not
exposed as a registered tool.
"""

from __future__ import annotations


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
