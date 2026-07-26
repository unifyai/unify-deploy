"""Unit tests for adapters dual-auth (admin OR owner-verified user key)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from common import adapter_auth as auth
from common.settings import SETTINGS


@pytest.fixture
def _admin_key(monkeypatch):
    monkeypatch.setattr(SETTINGS, "orchestra_admin_key", "ADMIN-KEY", raising=False)
    monkeypatch.setattr(
        SETTINGS,
        "orchestra_url",
        "https://orchestra.example.com/v0",
        raising=False,
    )
    return "ADMIN-KEY"


def _request(*, authorization: str | None) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    if authorization is not None:
        request.headers["Authorization"] = authorization
    request.state = SimpleNamespace()
    return request


@pytest.mark.asyncio
async def test_require_admin_or_user_key_accepts_admin(_admin_key):
    request = _request(authorization="Bearer ADMIN-KEY")
    await auth.require_admin_or_user_key(request)
    assert request.state.adapters_is_admin is True


@pytest.mark.asyncio
async def test_require_admin_or_user_key_rejects_missing_bearer(_admin_key):
    request = _request(authorization=None)
    with pytest.raises(HTTPException) as exc:
        await auth.require_admin_or_user_key(request)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_or_user_key_accepts_user_key(monkeypatch, _admin_key):
    request = _request(authorization="Bearer user-key")

    async def _auth(api_key: str):
        assert api_key == "user-key"
        return {"user_id": "u1"}

    monkeypatch.setattr(auth, "authenticate_user_api_key", _auth)
    await auth.require_admin_or_user_key(request)
    assert request.state.adapters_is_admin is False
    assert request.state.adapters_api_key == "user-key"
    assert request.state.adapters_user == {"user_id": "u1"}


@pytest.mark.asyncio
async def test_require_admin_or_user_key_rejects_invalid_user_key(
    monkeypatch,
    _admin_key,
):
    request = _request(authorization="Bearer bad-key")

    async def _auth(api_key: str):
        raise HTTPException(status_code=401, detail="Invalid API key.")

    monkeypatch.setattr(auth, "authenticate_user_api_key", _auth)
    with pytest.raises(HTTPException) as exc:
        await auth.require_admin_or_user_key(request)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_assistant_ownership_bypasses_admin(_admin_key):
    request = _request(authorization="Bearer ADMIN-KEY")
    request.state.adapters_is_admin = True
    await auth.require_assistant_ownership(request, "1406")


@pytest.mark.asyncio
async def test_require_assistant_ownership_allows_owner(monkeypatch, _admin_key):
    request = _request(authorization="Bearer user-key")
    request.state.adapters_is_admin = False
    request.state.adapters_api_key = "user-key"

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"info": [{"agent_id": "1406"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=response)

    with patch.object(auth.httpx, "AsyncClient", return_value=mock_client):
        await auth.require_assistant_ownership(request, "1406")

    mock_client.get.assert_awaited_once()
    kwargs = mock_client.get.await_args.kwargs
    assert kwargs["params"] == {"agent_id": "1406"}
    assert kwargs["headers"]["Authorization"] == "Bearer user-key"


@pytest.mark.asyncio
async def test_require_assistant_ownership_rejects_non_owner(monkeypatch, _admin_key):
    request = _request(authorization="Bearer user-key")
    request.state.adapters_is_admin = False
    request.state.adapters_api_key = "user-key"

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"info": []}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=response)

    with patch.object(auth.httpx, "AsyncClient", return_value=mock_client):
        with pytest.raises(HTTPException) as exc:
            await auth.require_assistant_ownership(request, "1406")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_assistant_ownership_rejects_unknown_assistant_id(_admin_key):
    request = _request(authorization="Bearer user-key")
    request.state.adapters_is_admin = False
    request.state.adapters_api_key = "user-key"
    with pytest.raises(HTTPException) as exc:
        await auth.require_assistant_ownership(request, "unknown")
    assert exc.value.status_code == 403
