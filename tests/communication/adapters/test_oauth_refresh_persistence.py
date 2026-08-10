from __future__ import annotations

import os
import sys
from pathlib import Path

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


def test_store_refreshed_oauth_secrets_uses_post_fallback(monkeypatch):
    from adapters import main

    calls: list[tuple[str, str]] = []

    def put(url, **kwargs):
        calls.append(("put", url))
        return _Response(404, "missing")

    def post(url, **kwargs):
        calls.append(("post", url))
        return _Response(201, "created")

    monkeypatch.setattr(main.SETTINGS, "orchestra_url", "https://orchestra.test")
    monkeypatch.setattr(main.requests, "put", put)
    monkeypatch.setattr(main.requests, "post", post)

    stored, error = main._store_refreshed_oauth_secrets(
        provider="fake",
        assistant_id=123,
        assistant_email="person@example.com",
        api_key="assistant-key",
        secrets_to_store={"FAKE_ACCESS_TOKEN": "fresh"},
    )

    assert stored is True
    assert error is None
    assert [method for method, _ in calls] == ["put", "post"]


def test_scheduled_google_token_failure_is_not_marked_refreshed(monkeypatch):
    from adapters import main

    def get(url, **kwargs):
        return _Response(
            200,
            payload={
                "info": [
                    {
                        "email": "assistant@example.com",
                        "agent_id": 42,
                        "api_key": "assistant-key",
                        "secrets": {
                            "GOOGLE_ACCESS_TOKEN": "old-access",
                            "GOOGLE_REFRESH_TOKEN": "refresh",
                        },
                    },
                ],
            },
        )

    def post(url, **kwargs):
        return _Response(
            200,
            payload={"access_token": "new-access", "expires_in": 3600},
        )

    def put(url, **kwargs):
        return _Response(500, "store failed")

    monkeypatch.setattr(main.SETTINGS, "orchestra_admin_key", "admin-key")
    monkeypatch.setattr(main.SETTINGS, "orchestra_url", "https://orchestra.test")
    monkeypatch.setattr(main.SETTINGS, "google_oauth_client_id", "google-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "put", put)

    result = main.scheduled_google_tokens(main.ScheduledPayload())

    assert result["refreshed"] == []
    assert result["failed"] == [
        {
            "email": "assistant@example.com",
            "error": "Failed to store google secret GOOGLE_ACCESS_TOKEN: store failed",
        },
    ]


def test_scheduled_microsoft_token_failure_is_not_marked_refreshed(monkeypatch):
    from adapters import main

    def get(url, **kwargs):
        return _Response(
            200,
            payload={
                "info": [
                    {
                        "email": "assistant@example.com",
                        "agent_id": 42,
                        "api_key": "assistant-key",
                        "secrets": {
                            "MICROSOFT_ACCESS_TOKEN": "old-access",
                            "MICROSOFT_REFRESH_TOKEN": "refresh",
                            "MICROSOFT_TOKEN_SOURCE": "byod",
                        },
                    },
                ],
            },
        )

    def post(url, **kwargs):
        return _Response(
            200,
            payload={"access_token": "new-access", "expires_in": 3600},
        )

    def put(url, **kwargs):
        return _Response(500, "store failed")

    monkeypatch.setattr(main.SETTINGS, "orchestra_admin_key", "admin-key")
    monkeypatch.setattr(main.SETTINGS, "orchestra_url", "https://orchestra.test")
    monkeypatch.setattr(main.SETTINGS, "ms365_byod_client_id", "ms-client")
    monkeypatch.setenv("MS365_BYOD_CLIENT_SECRET", "ms-secret")
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.requests, "put", put)

    result = main.scheduled_microsoft_tokens(main.ScheduledPayload())

    assert result["refreshed"] == []
    assert result["failed"] == [
        {
            "email": "assistant@example.com",
            "error": (
                "Failed to store microsoft secret "
                "MICROSOFT_ACCESS_TOKEN: store failed"
            ),
        },
    ]
