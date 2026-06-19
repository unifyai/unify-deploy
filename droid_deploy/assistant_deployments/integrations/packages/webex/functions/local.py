"""Local DataManager query helpers for Webex.

After ``run_webex_sync_tick`` has materialised data into ``Webex/...``
contexts, these functions are the preferred read path for analytical
queries.  Each returns a ``freshness`` block so the assistant knows
whether to trust the local copy.

Mirrors employment_hero/local.py.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def query_local_webex_meetings(
    host_email: str | None = None,
    state: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    title_contains: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    """Query the synced meetings table."""
    if mock:
        return {
            "rows": [
                {
                    "id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                    "title": "Battersea — Q2 ops review",
                    "start": "2026-05-06T10:00:00Z",
                    "end": "2026-05-06T11:00:00Z",
                    "host_email": "alex@example.test",
                    "state": "scheduled",
                },
            ],
            "count": 1,
            "freshness": {"is_fresh": True, "threshold_seconds": 1800},
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if host_email:
        filters.append(f"`host_email` == '{host_email.lower()}'")
    if state:
        filters.append(f"`state` == '{state}'")
    if from_iso:
        filters.append(f"`start` >= '{from_iso}'")
    if to_iso:
        filters.append(f"`end` <= '{to_iso}'")
    if title_contains:
        safe = title_contains.replace("'", "''")
        filters.append(f"`title` LIKE '%{safe}%'")

    rows = await safe_filter(
        dm,
        "Webex/Meetings",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("meetings"),
    }


@custom_function()
async def query_local_webex_recordings(
    host_email: str | None = None,
    meeting_id: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    """Query the synced recordings table.  Use the live API for
    short-lived download URLs."""
    if mock:
        return {
            "rows": [
                {
                    "id": "Y2lzY29zcGFyazovL3VzL1JFQ09SRElORy9tb2NrLTE",
                    "topic": "Maintenance crew weekly",
                    "create_time": "2026-05-04T08:30:00Z",
                    "duration_seconds": 1810,
                    "host_email": "alex@example.test",
                    "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                },
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if host_email:
        filters.append(f"`host_email` == '{host_email.lower()}'")
    if meeting_id:
        filters.append(f"`meeting_id` == '{meeting_id}'")
    if from_iso:
        filters.append(f"`create_time` >= '{from_iso}'")
    if to_iso:
        filters.append(f"`create_time` <= '{to_iso}'")

    rows = await safe_filter(
        dm,
        "Webex/Recordings",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("recordings"),
    }


@custom_function()
async def query_local_webex_transcripts(
    meeting_id: str | None = None,
    contains: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Query the synced transcripts table.

    Body text is only present when ``WEBEX_MIRROR_TRANSCRIPTS=true`` was
    set during the sync that produced these rows.
    """
    if mock:
        return {
            "rows": [
                {
                    "id": "Y2lzY29zcGFyazovL3VzL1RSQU5TQ1JJUFQvbW9jay0x",
                    "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                    "status": "available",
                    "language": "en-US",
                    "download_at": "2026-05-04T09:00:00Z",
                    "body": (
                        "Alex Example: Welcome everyone, this is the "
                        "maintenance crew weekly. Sam Sample: Three open "
                        "work orders at Battersea this week, all routine."
                    ),
                },
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
            "_note": (
                "Body text present only when WEBEX_MIRROR_TRANSCRIPTS=true "
                "during the producing sync."
            ),
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if meeting_id:
        filters.append(f"`meeting_id` == '{meeting_id}'")
    if contains:
        safe = contains.replace("'", "''")
        filters.append(f"`body` LIKE '%{safe}%'")
    if from_iso:
        filters.append(f"`download_at` >= '{from_iso}'")
    if to_iso:
        filters.append(f"`download_at` <= '{to_iso}'")

    rows = await safe_filter(
        dm,
        "Webex/Transcripts",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("transcripts"),
        "_note": (
            "Body text present only when WEBEX_MIRROR_TRANSCRIPTS=true "
            "during the producing sync."
        ),
    }


@custom_function()
async def query_local_webex_rooms(
    title_contains: str | None = None,
    creator_id: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    """Query the synced rooms table."""
    if mock:
        return {
            "rows": [
                {
                    "id": "Y2lzY29zcGFyazovL3VzL1JPT00vbW9jay0x",
                    "title": "Battersea Portfolio — Ops",
                    "type": "group",
                    "last_activity": "2026-05-04T15:00:00.000Z",
                },
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if title_contains:
        safe = title_contains.replace("'", "''")
        filters.append(f"`title` LIKE '%{safe}%'")
    if creator_id:
        filters.append(f"`creator_id` == '{creator_id}'")
    rows = await safe_filter(
        dm,
        "Webex/Rooms",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows), "freshness": await freshness("rooms")}


@custom_function()
async def query_local_webex_people(
    email: str | None = None,
    display_name_contains: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    """Query the synced people directory."""
    if mock:
        return {
            "rows": [
                {
                    "id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                    "email": "alex@example.test",
                    "display_name": "Alex Example",
                    "type": "person",
                },
            ],
            "count": 1,
            "freshness": {"is_fresh": True},
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()
    filters: list[str] = []
    if email:
        filters.append(f"`email` == '{email.lower()}'")
    if display_name_contains:
        safe = display_name_contains.replace("'", "''")
        filters.append(f"`display_name` LIKE '%{safe}%'")
    rows = await safe_filter(
        dm,
        "Webex/People",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("people"),
    }


# ---------------------------------------------------------------------------
# Cross-app join: Webex meetings ↔ HubSpot contacts (by attendee email)
# ---------------------------------------------------------------------------


@custom_function()
async def query_local_webex_meetings_with_hubspot_contacts(
    email: str | None = None,
    meeting_id: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    limit: int = 200,
    mock: bool = True,
) -> dict:
    """Join Webex meeting invitees to HubSpot contacts on lowercased email.

    Returns one row per (meeting, invitee, hubspot_contact) triple,
    plus the meeting title and contact name for human-readable
    read-out.  Use ``email`` to scope to a single contact, or
    ``meeting_id`` for the inverse.
    """
    if mock:
        return {
            "rows": [
                {
                    "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0x",
                    "meeting_title": "Battersea — Q2 ops review",
                    "meeting_start": "2026-05-06T10:00:00Z",
                    "meeting_state": "scheduled",
                    "invitee_email": "alex@example.test",
                    "invitee_display_name": "Alex Example",
                    "hubspot_contact_id": "12345",
                    "hubspot_first_name": "Alex",
                    "hubspot_last_name": "Example",
                    "hubspot_company": "Acme Property",
                },
            ],
            "count": 1,
            "freshness": {"meetings": {"is_fresh": True}},
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._local_helpers import (
        freshness,
    )

    dm = ManagerRegistry.get_data_manager()

    filters: list[str] = []
    if email:
        filters.append(f"inv.email == '{email.lower()}'")
    if meeting_id:
        filters.append(f"m.id == '{meeting_id}'")
    if from_iso:
        filters.append(f"m.start >= '{from_iso}'")
    if to_iso:
        filters.append(f"m.end <= '{to_iso}'")

    try:
        rows = await dm.filter_join(
            tables=[
                ("Webex/Meetings", "m"),
                ("Webex/Meetings/Invitees", "inv"),
                ("HubSpot/CRM/Dimensions/Contacts", "hc"),
            ],
            join_expr=("inv.meeting_id == m.id AND inv.email == hc.email"),
            filter=" AND ".join(filters) if filters else None,
            select=[
                "m.id AS meeting_id",
                "m.title AS meeting_title",
                "m.start AS meeting_start",
                "m.end AS meeting_end",
                "m.state AS meeting_state",
                "inv.email AS invitee_email",
                "inv.display_name AS invitee_display_name",
                "hc.hubspot_id AS hubspot_contact_id",
                "hc.first_name AS hubspot_first_name",
                "hc.last_name AS hubspot_last_name",
                "hc.company AS hubspot_company",
            ],
            order_by="m.start desc",
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error": f"Local join failed: {e!r}",
            "hint": (
                "DataManager filter_join may not be available, or "
                "either Webex/Meetings/Invitees or HubSpot/CRM/Dimensions/"
                "Contacts hasn't been synced yet.  Run "
                "run_webex_sync_tick(full=true) and "
                "run_hubspot_sync_tick(full=true) first."
            ),
            "freshness": {
                "meetings": await freshness("meetings"),
            },
        }

    return {
        "rows": rows or [],
        "count": len(rows or []),
        "freshness": {"meetings": await freshness("meetings")},
    }
