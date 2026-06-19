"""Fetch linked user desktops for self-host ConversationManager startup."""

from __future__ import annotations

import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request


def _orchestra_base_url() -> str:
    return os.environ.get("ORCHESTRA_URL", "http://orchestra:8000/v0").rstrip("/")


def _admin_key() -> str:
    return os.environ.get("ORCHESTRA_ADMIN_KEY", "").strip()


def fetch_user_desktops(agent_id: str) -> list[dict[str, object]]:
    """Return the ``user_desktops`` list for ``agent_id`` from Orchestra admin API."""

    admin_key = _admin_key()
    if not admin_key:
        return []

    query = urllib.parse.urlencode(
        {
            "agent_id": agent_id,
            "from_fields": "agent_id,user_desktops",
        },
    )
    req = urllib.request.Request(
        f"{_orchestra_base_url()}/admin/assistant?{query}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.load(resp)
    except urllib.error.HTTPError:
        return []

    items = raw.get("info") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    for item in items:
        if str(item.get("agent_id") or item.get("agentId") or "") != str(agent_id):
            continue
        desktops = item.get("user_desktops") or []
        if isinstance(desktops, list):
            return desktops
        return []
    return []


def encode_user_desktops(desktops: list[dict[str, object]]) -> str:
    """Encode desktop links for ``ASSISTANT_USER_DESKTOPS``."""

    if not desktops:
        return ""
    return json.dumps(desktops, separators=(",", ":"))


def main() -> int:
    if len(sys.argv) < 2:
        print("", file=sys.stdout)
        return 0

    agent_id = sys.argv[1]
    export_sh = "--export-sh" in sys.argv[2:]
    payload = encode_user_desktops(fetch_user_desktops(agent_id))

    if export_sh:
        if payload:
            print(f"export ASSISTANT_USER_DESKTOPS={shlex.quote(payload)}")
        else:
            print("export ASSISTANT_USER_DESKTOPS=")
        return 0

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
