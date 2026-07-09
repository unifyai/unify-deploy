"""Unit tests for per-assistant / dual auth in communication.dependencies."""

from __future__ import annotations

import pytest

from common.settings import SETTINGS
from communication import dependencies as deps


@pytest.fixture
def _admin_key(monkeypatch):
    monkeypatch.setattr(SETTINGS, "orchestra_admin_key", "ADMIN-KEY", raising=False)
    return "ADMIN-KEY"


@pytest.mark.asyncio
async def test_authorize_admin_short_circuits(monkeypatch, _admin_key):
    monkeypatch.setattr(deps, "extract_api_key", lambda request: "ADMIN-KEY")

    async def _should_not_run(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("verify_assistant_session must not run for admin key")

    monkeypatch.setattr(deps, "verify_assistant_session", _should_not_run)

    ctx = await deps.authorize_admin_or_assistant(object(), assistant_id=7367)

    assert ctx.is_admin is True
    assert ctx.identity is None


@pytest.mark.asyncio
async def test_authorize_falls_back_to_assistant_session(monkeypatch, _admin_key):
    monkeypatch.setattr(deps, "extract_api_key", lambda request: "assistant-key")

    identity = deps.AssistantSessionIdentity(
        assistant_id=7367,
        api_key="assistant-key",
        org_id=5,
        user_id="u1",
        team_ids=[1, 2],
    )

    async def _verify(*, api_key, assistant_id, requested_binding_id=None):
        assert api_key == "assistant-key"
        assert assistant_id == 7367
        return identity

    monkeypatch.setattr(deps, "verify_assistant_session", _verify)

    ctx = await deps.authorize_admin_or_assistant(object(), assistant_id=7367)

    assert ctx.is_admin is False
    assert ctx.identity is identity


@pytest.mark.asyncio
async def test_authorize_rejects_wrong_assistant(monkeypatch, _admin_key):
    from fastapi import HTTPException

    monkeypatch.setattr(deps, "extract_api_key", lambda request: "other-assistant-key")

    async def _verify(*, api_key, assistant_id, requested_binding_id=None):
        raise HTTPException(status_code=401, detail="mismatch")

    monkeypatch.setattr(deps, "verify_assistant_session", _verify)

    with pytest.raises(HTTPException) as exc:
        await deps.authorize_admin_or_assistant(object(), assistant_id=9999)
    assert exc.value.status_code == 401
