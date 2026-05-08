"""Webex people directory — live reads + sync snapshot.

People is the directory of Webex users visible to the connected
account.  The ``email`` field is the join key for cross-app linking
(HubSpot contacts, Employment Hero employees, etc.).
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_webex_me(mock: bool = True) -> dict:
    """Return identity for the connected Webex user.

    Hits ``/v1/people/me`` — useful as a connectivity probe.
    """
    if mock:
        return {
            "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLW1l",
            "emails": ["operator@example.test"],
            "displayName": "Mock Operator",
            "type": "person",
            "orgId": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9tb2Nr",
            "created": "2026-04-01T10:00:00.000Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    body = await webex_get("/v1/people/me")
    return body


@custom_function()
async def get_webex_person(person_id: str, mock: bool = True) -> dict:
    """Get a single Webex person by id."""
    if mock:
        return {
            "id": str(person_id),
            "emails": ["alex@example.test"],
            "displayName": "Alex Example",
            "firstName": "Alex",
            "lastName": "Example",
            "type": "person",
            "orgId": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9tb2Nr",
            "created": "2025-09-01T10:00:00.000Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    return await webex_get(f"/v1/people/{person_id}")


@custom_function()
async def list_webex_people(
    email: str | None = None,
    display_name: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List Webex people in the org.

    Webex's ``/v1/people`` always requires a filter — ``email``,
    ``displayName``, ``id``, or ``orgId``.  When the caller supplies
    neither ``email`` nor ``display_name``, this falls back to looking
    up the connected user's ``orgId`` via ``/v1/people/me`` and listing
    by org (the admin-style enumeration path).  Without admin scope the
    org-wide listing returns 403, which we surface via the standard
    envelope.
    """
    if mock:
        rows = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                "emails": ["alex@example.test"],
                "displayName": "Alex Example",
                "firstName": "Alex",
                "lastName": "Example",
                "type": "person",
            },
            {
                "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTI",
                "emails": ["sam@example.test"],
                "displayName": "Sam Sample",
                "firstName": "Sam",
                "lastName": "Sample",
                "type": "person",
            },
        ]
        return {"people": rows, "count": len(rows)}

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    params: dict = {"max": min(limit, 100)}
    if email:
        params["email"] = email
    elif display_name:
        params["displayName"] = display_name
    else:
        me = await webex_get("/v1/people/me")
        if "error" in me:
            return me
        org_id = me.get("orgId")
        if not org_id:
            return {
                "error": "Webex /v1/people requires a filter; /people/me did not return orgId.",
                "status_code": 400,
                "hint": (
                    "Pass ``email`` or ``display_name``, or reconnect Webex with a token that "
                    "exposes ``orgId`` on /people/me."
                ),
            }
        params["orgId"] = org_id

    body = await webex_get("/v1/people", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"people": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
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
            }
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
