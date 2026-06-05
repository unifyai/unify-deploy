"""Unit tests for Discord gateway startup dispatch."""

import pytest

from communication.discord.gateway import _ensure_job_running


class _Response:
    status_code = 200
    text = ""


class _AsyncClient:
    posted_data = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, _url, *, headers, data, timeout):
        self.__class__.posted_data = data
        return _Response()


@pytest.mark.asyncio
async def test_discord_start_job_form_carries_coordinator_flag(monkeypatch):
    """Discord startup should form-encode the raw Orchestra Coordinator flag."""

    monkeypatch.setattr(
        "communication.discord.gateway.httpx.AsyncClient",
        _AsyncClient,
    )

    await _ensure_job_running(
        {
            "agent_id": "assistant-123",
            "api_key": "test-api-key",
            "user_id": "user-123",
            "first_name": "Test",
            "surname": "Assistant",
            "is_coordinator": True,
        },
    )

    assert _AsyncClient.posted_data["is_coordinator"] == "true"
