"""Webex people sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_webex_sync_tick`` orchestrator via importlib; not
exposed as a registered tool.
"""

from __future__ import annotations


async def sync_webex_people(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot the Webex people directory into the canonical
    ``{schema_version, tables, metadata}`` envelope.

    Webex's ``/v1/people`` doesn't expose a generic ``updated_since``
    filter without admin scopes, so this is a full pull on every run
    when admin-listing isn't available.  With admin scopes, the
    ``orgId`` filter pulls all people in the org.
    """
    import datetime as _dt

    schema_version = "webex.people.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        people = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                "email": "alex@example.test",
                "display_name": "Alex Example",
                "first_name": "Alex",
                "last_name": "Example",
                "type": "person",
                "org_id": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9tb2Nr",
                "created_at": "2025-09-01T10:00:00.000Z",
            },
            {
                "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTI",
                "email": "sam@example.test",
                "display_name": "Sam Sample",
                "first_name": "Sam",
                "last_name": "Sample",
                "type": "person",
                "org_id": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9tb2Nr",
                "created_at": "2025-09-01T10:00:00.000Z",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"webex_people": people},
            "metadata": {
                "integration": "webex",
                "object_type": "people",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    # Webex's ``/v1/people`` always requires a filter.  Look up the
    # connected user's ``orgId`` and enumerate by org — this is the
    # admin-style listing path; without admin scope Webex returns 403,
    # which the client wraps in the standard envelope.
    me = await webex_get("/v1/people/me")
    if "error" in me:
        me.update({"schema_version": schema_version, "tables": {}})
        return me
    org_id = me.get("orgId")
    if not org_id:
        return {
            "schema_version": schema_version,
            "tables": {},
            "error": "Webex /people/me did not return orgId — cannot enumerate people directory.",
            "status_code": None,
        }

    body = await webex_get("/v1/people", params={"orgId": org_id, "max": 100})
    if "error" in body:
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    items = body.get("items") or []
    people: list[dict] = []
    for p in items:
        emails = p.get("emails") or []
        people.append(
            {
                "id": p.get("id"),
                "email": (emails[0] if emails else None),
                "emails_json": str(emails),
                "display_name": p.get("displayName"),
                "first_name": p.get("firstName"),
                "last_name": p.get("lastName"),
                "type": p.get("type"),
                "org_id": p.get("orgId"),
                "status": p.get("status"),
                "last_activity": p.get("lastActivity"),
                "created_at": p.get("created"),
            },
        )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"webex_people": people},
        "metadata": {
            "integration": "webex",
            "object_type": "people",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"webex_people": len(people)},
        },
    }
