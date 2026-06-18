"""Fetch assistant fields from Orchestra for self-host CM startup."""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 4:
        print("", file=sys.stdout)
        return 0
    unify_key, agent_id, field = sys.argv[1], sys.argv[2], sys.argv[3]
    orchestra_url = os.environ.get("ORCHESTRA_URL", "http://orchestra:8000/v0").rstrip(
        "/",
    )
    req = urllib.request.Request(
        f"{orchestra_url}/assistant",
        headers={"Authorization": f"Bearer {unify_key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.load(resp)
    items = raw.get("info") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return 0
    for item in items:
        if str(item.get("agent_id") or item.get("agentId") or "") != str(agent_id):
            continue
        value = item
        for part in field.split("."):
            if not part:
                continue
            value = value.get(part) if isinstance(value, dict) else ""
        print(value or "")
        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
