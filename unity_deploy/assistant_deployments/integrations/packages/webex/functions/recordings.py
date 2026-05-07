"""Webex recordings — live reads + sync snapshot.

Mirrors recording **metadata** only — never the audio/video bytes.
The download URL has a short TTL and must be re-fetched when needed.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_webex_recordings(
    from_iso: str | None = None,
    to_iso: str | None = None,
    meeting_id: str | None = None,
    host_email: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List recordings, optionally scoped by date range, meeting, or host."""
    if mock:
        rows = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1JFQ09SRElORy9tb2NrLTE",
                "topic": "Maintenance crew weekly",
                "createTime": "2026-05-04T08:30:00Z",
                "timeRecorded": "2026-05-04T08:00:00Z",
                "format": "MP4",
                "durationSeconds": 1810,
                "sizeBytes": 84_312_192,
                "hostEmail": "alex@example.test",
                "meetingId": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                "downloadUrl": "https://example.webex.com/recordings/mock-download-url",
                "playbackUrl": "https://example.webex.com/recordings/mock-playback-url",
            },
        ]
        return {"recordings": rows, "count": len(rows)}

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
        params["from"] = lookback.isoformat()
    if to_iso:
        params["to"] = to_iso
    if meeting_id:
        params["meetingId"] = meeting_id
    if host_email:
        params["hostEmail"] = host_email

    body = await webex_get("/v1/recordings", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"recordings": items, "count": len(items)}


@custom_function()
async def get_webex_recording(recording_id: str, mock: bool = True) -> dict:
    """Get one recording by id (returns metadata + short-lived URLs)."""
    if mock:
        return {
            "id": str(recording_id),
            "topic": "Maintenance crew weekly",
            "createTime": "2026-05-04T08:30:00Z",
            "timeRecorded": "2026-05-04T08:00:00Z",
            "format": "MP4",
            "durationSeconds": 1810,
            "sizeBytes": 84_312_192,
            "hostEmail": "alex@example.test",
            "meetingId": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
            "downloadUrl": "https://example.webex.com/recordings/mock-download-url",
            "playbackUrl": "https://example.webex.com/recordings/mock-playback-url",
            "temporaryDirectDownloadLinks": {
                "audioDownloadLink": None,
                "videoDownloadLink": None,
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    return await webex_get(f"/v1/recordings/{recording_id}")


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
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
        params["from"] = lookback.isoformat()

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
            }
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
