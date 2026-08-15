"""Unit tests for per-assistant / dual auth in communication.dependencies."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from communication import dependencies as deps


@pytest.fixture
def _admin_key(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "ADMIN-KEY")
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
    monkeypatch.setattr(deps, "extract_api_key", lambda request: "other-assistant-key")

    async def _verify(*, api_key, assistant_id, requested_binding_id=None):
        raise HTTPException(status_code=401, detail="mismatch")

    monkeypatch.setattr(deps, "verify_assistant_session", _verify)

    with pytest.raises(HTTPException) as exc:
        await deps.authorize_admin_or_assistant(object(), assistant_id=9999)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_assistant_identity_from_key_success(monkeypatch):
    async def _auth(api_key: str):
        assert api_key == "assistant-key"
        return {"user_id": "u1"}

    def _lookup(*, assistant_id: str):
        assert assistant_id == "1406"
        return {
            "assistant_id": "1406",
            "api_key": "assistant-key",
            "org_id": 1,
            "user_id": "u1",
            "team_ids": [11],
        }

    monkeypatch.setattr(deps, "authenticate_user_api_key", _auth)
    monkeypatch.setattr(
        "common.assistant_lookup.get_assistant",
        _lookup,
    )

    identity = await deps.verify_assistant_identity_from_key(
        api_key="assistant-key",
        assistant_id=1406,
    )

    assert identity.assistant_id == 1406
    assert identity.api_key == "assistant-key"
    assert identity.org_id == 1
    assert identity.user_id == "u1"
    assert identity.team_ids == [11]


@pytest.mark.asyncio
async def test_verify_assistant_identity_from_key_wrong_key(monkeypatch):
    async def _auth(api_key: str):
        return {"user_id": "u1"}

    def _lookup(*, assistant_id: str):
        return {
            "assistant_id": "1406",
            "api_key": "correct-key",
            "org_id": 1,
            "user_id": "u1",
            "team_ids": [],
        }

    monkeypatch.setattr(deps, "authenticate_user_api_key", _auth)
    monkeypatch.setattr("common.assistant_lookup.get_assistant", _lookup)

    with pytest.raises(HTTPException) as exc:
        await deps.verify_assistant_identity_from_key(
            api_key="wrong-key",
            assistant_id=1406,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_assistant_identity_from_key_missing_assistant(monkeypatch):
    async def _auth(api_key: str):
        return {"user_id": "u1"}

    def _lookup(*, assistant_id: str):
        return {"assistant_id": None, "api_key": ""}

    monkeypatch.setattr(deps, "authenticate_user_api_key", _auth)
    monkeypatch.setattr("common.assistant_lookup.get_assistant", _lookup)

    with pytest.raises(HTTPException) as exc:
        await deps.verify_assistant_identity_from_key(
            api_key="assistant-key",
            assistant_id=1406,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_verify_assistant_identity_from_key_rejects_invalid_user_key(
    monkeypatch,
):
    async def _auth(api_key: str):
        raise HTTPException(status_code=401, detail="Invalid API key.")

    def _should_not_lookup(**kwargs):  # pragma: no cover
        raise AssertionError("get_assistant must not run when user key is invalid")

    monkeypatch.setattr(deps, "authenticate_user_api_key", _auth)
    monkeypatch.setattr("common.assistant_lookup.get_assistant", _should_not_lookup)

    with pytest.raises(HTTPException) as exc:
        await deps.verify_assistant_identity_from_key(
            api_key="ADMIN-KEY",
            assistant_id=1406,
        )
    assert exc.value.status_code == 401
