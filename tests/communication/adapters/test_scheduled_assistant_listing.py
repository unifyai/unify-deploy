from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

os.environ.setdefault("OUTLOOK_WEBHOOK_SECRET", "test-outlook-secret")
os.environ.setdefault("OAUTH_STATE_SIGNING_KEY", "test-oauth-signing-key")


class _Response:
    def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_list_scheduled_assistants_scopes_fields_and_returns_info(monkeypatch):
    from adapters import main

    captured: dict = {}

    def get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["timeout"] = kwargs.get("timeout")
        return _Response(200, payload={"info": [{"agent_id": 1}, {"agent_id": 2}]})

    monkeypatch.setenv("ORCHESTRA_URL", "https://orchestra.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(main.requests, "get", get)

    out = main._list_scheduled_assistants(
        from_fields="agent_id,email,secrets",
        timeout=120,
        caller="unit",
    )

    assert [a["agent_id"] for a in out] == [1, 2]
    assert captured["url"].endswith("/admin/assistant")
    assert captured["params"]["from_fields"] == "agent_id,email,secrets"
    assert captured["timeout"] == 120


def test_list_scheduled_assistants_retries_then_succeeds(monkeypatch):
    from adapters import main

    calls = {"n": 0}

    def get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("transient network error")
        return _Response(200, payload={"info": [{"agent_id": 7}]})

    monkeypatch.setenv("ORCHESTRA_URL", "https://orchestra.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.time, "sleep", lambda *_a, **_k: None)

    out = main._list_scheduled_assistants(retries=2, caller="unit")

    assert [a["agent_id"] for a in out] == [7]
    assert calls["n"] == 2


def test_list_scheduled_assistants_raises_after_exhaustion(monkeypatch):
    from adapters import main

    def get(url, **kwargs):
        return _Response(500, text="upstream boom")

    monkeypatch.setenv("ORCHESTRA_URL", "https://orchestra.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="Failed to list assistants"):
        main._list_scheduled_assistants(retries=1, caller="unit")


def test_list_scheduled_assistants_paginates_with_limit(monkeypatch):
    from adapters import main

    everyone = [{"agent_id": i} for i in range(5)]
    pages: list[int] = []

    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 0))
        pages.append(offset)
        return _Response(200, payload={"info": everyone[offset : offset + limit]})

    monkeypatch.setenv("ORCHESTRA_URL", "https://orchestra.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(main.requests, "get", get)

    out = main._list_scheduled_assistants(limit=2, retries=0, caller="unit")

    assert [a["agent_id"] for a in out] == [0, 1, 2, 3, 4]
    assert pages == [0, 2, 4]


def test_scheduled_teams_watches_filters_by_email_provider(monkeypatch):
    from adapters import main

    def get(url, **kwargs):
        return _Response(
            200,
            payload={
                "info": [
                    {"email": "ms@example.com", "email_provider": "microsoft_365"},
                    {"email": "g@example.com", "email_provider": "google_workspace"},
                    {"email": "legacy@example.com"},  # no provider -> defaults gmail
                ],
            },
        )

    posted: list[str] = []

    def post(url, **kwargs):
        posted.append((kwargs.get("json") or {}).get("primary_email"))
        return _Response(200, payload={"success": True})

    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "https://orchestra.test")
    monkeypatch.setenv("UNITY_COMMS_URL", "https://comms.test")
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", post)

    result = main.scheduled_teams_watches(main.ScheduledPayload())

    # Only the microsoft_365 assistant gets a Teams watch.
    assert posted == ["ms@example.com"]
    assert [r["email"] for r in result["renewed"]] == ["ms@example.com"]
