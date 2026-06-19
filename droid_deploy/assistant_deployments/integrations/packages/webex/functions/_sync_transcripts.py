"""Webex transcripts sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_webex_sync_tick`` orchestrator via importlib; not
exposed as a registered tool.

Transcripts are generated post-meeting after Webex's ASR pipeline runs
(typically 5–30 minutes after a meeting ends).  Bodies are mirrored
into DataManager only when ``WEBEX_MIRROR_TRANSCRIPTS=true`` — they
can be sensitive (operational chatter, named individuals) and
high-volume.  When the mirror is off, transcript metadata is mirrored
but body text is fetched live on demand only via the ``webex_request``
tool against ``/v1/meetingTranscripts/{id}/download``.
"""

from __future__ import annotations


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

    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )
    from droid_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
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
