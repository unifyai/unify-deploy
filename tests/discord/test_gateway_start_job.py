"""Tests for Discord runtime start requests."""

import json

import pytest

from communication.discord import gateway


class _Response:
    status_code = 200
    text = "ok"


class _AsyncClient:
    instances: list["_AsyncClient"] = []

    def __init__(self):
        self.posts: list[dict] = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _Response()


@pytest.mark.asyncio
async def test_discord_start_job_encodes_space_ids_for_form(monkeypatch):
    """Discord wakeups forward membership ids through the start-job form."""

    _AsyncClient.instances = []
    monkeypatch.setattr(gateway.httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(gateway.SETTINGS, "comms_url", "http://comms.test")
    monkeypatch.setattr(gateway.SETTINGS, "orchestra_admin_key", "admin-key")

    await gateway._ensure_job_running(
        {
            "agent_id": "assistant-123",
            "api_key": "test-api-key",
            "user_id": "user-123",
            "user_first_name": "Test",
            "user_last_name": "User",
            "user_email": "test@example.com",
            "first_name": "Test",
            "surname": "Assistant",
            "age": 25,
            "nationality": "US",
            "about": "Test assistant",
            "timezone": "UTC",
            "user_phone": "+1234567890",
            "phone": "+1987654321",
            "email": "assistant@example.com",
            "user_whatsapp_number": "+1234567890",
            "assistant_whatsapp_number": "+1987654321",
            "voice_provider": "elevenlabs",
            "voice_id": "voice-123",
            "desktop_mode": "ubuntu",
            "space_ids": [8, 9],
            "space_summaries": [
                {
                    "space_id": 8,
                    "name": "Discord Ops",
                    "description": "Discord workspace for support escalations.",
                },
            ],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
    )

    post = _AsyncClient.instances[0].posts[0]
    assert post["url"] == "http://comms.test/infra/job/start"
    assert json.loads(post["data"]["space_ids"]) == [8, 9]
    assert json.loads(post["data"]["space_summaries"]) == [
        {
            "space_id": 8,
            "name": "Discord Ops",
            "description": "Discord workspace for support escalations.",
        },
    ]
    assert json.loads(post["data"]["team_ids"]) == []
    assert post["data"]["self_contact_id"] == "42"
    assert post["data"]["boss_contact_id"] == "43"
