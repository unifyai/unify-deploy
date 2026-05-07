"""Webex post-meeting transcripts — live reads + opt-in body mirror.

Transcripts are generated post-meeting after Webex's ASR pipeline
runs.  Lag is typically 5–30 minutes after the meeting ends.

The package mirrors transcript **metadata** by default; transcript
**bodies** are mirrored only when ``WEBEX_MIRROR_TRANSCRIPTS=true`` —
they can be sensitive (operational chatter, named individuals) and
high-volume.  When the mirror is off, ``get_webex_meeting_transcript``
fetches body text live on demand.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_webex_meeting_transcripts(
    meeting_id: str | None = None,
    from_iso: str | None = None,
    to_iso: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List meeting transcripts (metadata only).

    Each transcript belongs to a meeting; bodies are fetched separately
    via ``get_webex_meeting_transcript``.
    """
    if mock:
        rows = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1RSQU5TQ1JJUFQvbW9jay0x",
                "meetingId": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                "hostUserId": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                "siteUrl": "example.webex.com",
                "vttDownloadLink": "https://example.webex.com/transcripts/mock-vtt",
                "txtDownloadLink": "https://example.webex.com/transcripts/mock-txt",
                "status": "available",
                "language": "en-US",
                "downloadAt": "2026-05-04T09:00:00Z",
            },
        ]
        return {"transcripts": rows, "count": len(rows)}

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    params: dict = {"max": min(limit, 100)}
    if meeting_id:
        params["meetingId"] = meeting_id
    if from_iso:
        params["from"] = from_iso
    if to_iso:
        params["to"] = to_iso

    body = await webex_get("/v1/meetingTranscripts", params=params)
    if "error" in body:
        return body
    items = body.get("items") or []
    return {"transcripts": items, "count": len(items)}


@custom_function()
async def get_webex_meeting_transcript(
    transcript_id: str,
    format: str = "txt",
    mock: bool = True,
) -> dict:
    """Get the body of a meeting transcript.

    ``format`` is ``"txt"`` (plain) or ``"vtt"`` (timestamped).  Returns
    ``{"transcript_id", "format", "body"}`` on success.
    """
    if mock:
        return {
            "transcript_id": str(transcript_id),
            "format": format,
            "body": (
                "00:00:01.000 --> 00:00:05.000\n"
                "Alex Example: Welcome everyone, this is the maintenance "
                "crew weekly.\n"
                "00:00:05.000 --> 00:00:10.000\n"
                "Sam Sample: Three open work orders at Battersea this "
                "week, all routine."
                if format == "vtt"
                else "Alex Example: Welcome everyone, this is the maintenance "
                "crew weekly. Sam Sample: Three open work orders at "
                "Battersea this week, all routine."
            ),
            "language": "en-US",
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    # Webex returns transcript body via download endpoint with format
    # query.  Falls back to txt on unknown format value.
    fmt = "vtt" if str(format).lower() == "vtt" else "txt"
    body = await webex_get(
        f"/v1/meetingTranscripts/{transcript_id}/download",
        params={"format": fmt},
    )
    if isinstance(body, dict) and "error" in body:
        return body
    return {
        "transcript_id": transcript_id,
        "format": fmt,
        "body": (
            body
            if isinstance(body, str)
            else (body.get("body") if isinstance(body, dict) else "")
        ),
    }


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
async def sync_webex_transcripts(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot transcript metadata into the canonical envelope.

    When ``WEBEX_MIRROR_TRANSCRIPTS=true``, also fetches each
    transcript's body and includes it in the row.  Off by default.
    """
    import datetime as _dt

    schema_version = "webex.transcripts.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        transcripts = [
            {
                "id": "Y2lzY29zcGFyazovL3VzL1RSQU5TQ1JJUFQvbW9jay0x",
                "meeting_id": "Y2lzY29zcGFyazovL3VzL01FRVRJTkcvbW9jay0y",
                "host_user_id": "Y2lzY29zcGFyazovL3VzL1BFT1BMRS9tb2NrLTE",
                "site_url": "example.webex.com",
                "status": "available",
                "language": "en-US",
                "download_at": "2026-05-04T09:00:00Z",
                "body": None,  # mirror flag off in mock
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"transcripts": transcripts},
            "metadata": {
                "integration": "webex",
                "object_type": "transcripts",
                "started_at": started,
                "mode": "mock",
                "mirror_bodies": False,
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

    body = await webex_get("/v1/meetingTranscripts", params=params)
    if "error" in body:
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    items = body.get("items") or []
    transcripts: list[dict] = []
    for t in items:
        row: dict = {
            "id": t.get("id"),
            "meeting_id": t.get("meetingId"),
            "host_user_id": t.get("hostUserId"),
            "site_url": t.get("siteUrl"),
            "status": t.get("status"),
            "language": t.get("language"),
            "download_at": t.get("downloadAt"),
        }
        if cfg["mirror_transcripts"] and t.get("status") == "available":
            txt = await webex_get(
                f"/v1/meetingTranscripts/{t.get('id')}/download",
                params={"format": "txt"},
            )
            row["body"] = (
                txt
                if isinstance(txt, str)
                else (
                    txt.get("body")
                    if isinstance(txt, dict) and "error" not in txt
                    else None
                )
            )
        else:
            row["body"] = None
        transcripts.append(row)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"transcripts": transcripts},
        "metadata": {
            "integration": "webex",
            "object_type": "transcripts",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"transcripts": len(transcripts)},
            "mirror_bodies": cfg["mirror_transcripts"],
        },
    }
