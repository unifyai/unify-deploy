"""Webex recordings sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_webex_sync_tick`` orchestrator via importlib; not
exposed as a registered tool.
"""

from __future__ import annotations


async def sync_webex_recordings(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot recording metadata into the canonical envelope.

    Bytes are not synced — the ``download_url`` field has a short TTL
    and must be re-fetched live when needed.
    """
    import datetime as _dt

    schema_version = "webex.recordings.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        recordings = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1JFQ09SRElORy9tb2NrLTE",
                "topic": "Maintenance crew weekly",
                "create_time": "2026-05-04T08:30:00Z",
                "time_recorded": "2026-05-04T08:00:00Z",
                "format": "MP4",
                "duration_seconds": 1810,
                "size_bytes": 84_312_192,
                "host_email": "alex@example.test",
                "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                "service_type": "MeetingCenter",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"recordings": recordings},
            "metadata": {
                "integration": "webex",
                "object_type": "recordings",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )
    from unify_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
        get_webex_config,
    )

    cfg = get_webex_config()
    params: dict = {"max": cfg["api_page_size"]}
    if since:
        params["from"] = since
    else:
        lookback = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(
            days=cfg["meeting_lookback_days"],
        )
        # Webex rejects ``+00:00``; needs the ``Z`` UTC suffix.
        params["from"] = lookback.strftime("%Y-%m-%dT%H:%M:%SZ")

    body = await webex_get("/v1/recordings", params=params)
    if "error" in body:
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    items = body.get("items") or []
    recordings: list[dict] = []
    for r in items:
        recordings.append(
            {
                "id": r.get("id"),
                "topic": r.get("topic"),
                "create_time": r.get("createTime"),
                "time_recorded": r.get("timeRecorded"),
                "format": r.get("format"),
                "duration_seconds": r.get("durationSeconds"),
                "size_bytes": r.get("sizeBytes"),
                "host_email": (r.get("hostEmail") or "").lower() or None,
                "meeting_id": r.get("meetingId"),
                "service_type": r.get("serviceType"),
                "site_url": r.get("siteUrl"),
            },
        )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"recordings": recordings},
        "metadata": {
            "integration": "webex",
            "object_type": "recordings",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"recordings": len(recordings)},
        },
    }
