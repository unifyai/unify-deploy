"""Tier / capability probe for Webex.

Sweeps representative endpoints for each sync object type and records
which return 200 vs. 403 / not-supported.  Cached in DataManager at
``Webex/Meta/Capabilities`` for ``WEBEX_TIER_PROBE_TTL_SECONDS``
(default 24h).

Underscore-prefixed so FunctionManager skips this file at discovery.
The runtime-callable wrapper lives in ``sync.py`` as ``probe_webex_tier``.
"""

from __future__ import annotations

# Probe endpoint per sync-object key.  Each is a cheap GET that should
# succeed if the connected user's token has the relevant scope.
_PROBE_ENDPOINTS: dict[str, str] = {
    "people": "/v1/people/me",
    "rooms": "/v1/rooms?max=1",
    "meetings": "/v1/meetings?max=1",
    "recordings": "/v1/recordings?max=1",
    "transcripts": "/v1/meetingTranscripts?max=1",
}


async def run_tier_probe() -> dict:
    """Sweep every probe endpoint; return ``{object_key: status_code}``.

    Caller is responsible for caching the result in DataManager.
    """
    from unify_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_get,
    )

    results: dict[str, dict] = {}
    for key, path in _PROBE_ENDPOINTS.items():
        body = await webex_get(path)
        if "error" in body:
            status = body.get("status_code")
            results[key] = {
                "available": False,
                "status_code": status,
                "hint": body.get("hint"),
            }
        else:
            results[key] = {"available": True, "status_code": 200}
    return results
