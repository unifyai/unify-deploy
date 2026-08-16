"""Unit tests that client-bundle auth uses Orchestra identity, not sessions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from communication.dependencies import AssistantSessionIdentity
from communication.infra import views


class _FakeRequest:
    def __init__(self, api_key: str):
        self.headers = {"authorization": f"Bearer {api_key}"}


@pytest.mark.asyncio
async def test_client_bundle_succeeds_without_assistant_session(monkeypatch):
    """Offline pods have UNIFY_KEY but no live AssistantSession — must still work."""

    identity = AssistantSessionIdentity(
        assistant_id=1406,
        api_key="assistant-key",
        org_id=1,
        user_id="u1",
        team_ids=[11],
    )

    async def _verify(*, api_key, assistant_id):
        assert api_key == "assistant-key"
        assert assistant_id == 1406
        return identity

    target = SimpleNamespace(
        client_name="unify_company",
        deployment="default",
        bundle_key="unify_company",
    )

    class _Blob:
        def __init__(self, text: str = "", exists: bool = True):
            self._text = text
            self._exists = exists

        def exists(self):
            return self._exists

        def download_as_text(self):
            return self._text

        def generate_signed_url(self, **kwargs):
            return "https://signed.example/bundle.tar.gz"

    class _Bucket:
        def blob(self, name: str):
            if name.endswith("latest.txt"):
                return _Blob("abc123")
            if name.endswith(".sha256"):
                return _Blob("deadbeef")
            return _Blob()

    class _StorageClient:
        def __init__(self, **kwargs):
            pass

        def bucket(self, name: str):
            return _Bucket()

    monkeypatch.setattr(views, "verify_assistant_identity_from_key", _verify)
    monkeypatch.setattr(
        "unify_deploy.assistant_deployments.routing_manifest."
        "resolve_client_bundle_target",
        lambda **kwargs: target,
    )
    monkeypatch.setattr(views, "storage", SimpleNamespace(Client=_StorageClient))
    monkeypatch.setattr(views, "_service_account_credentials", lambda: None)
    monkeypatch.setenv("UNITY_CLIENT_BUNDLE_BUCKET", "unity-client-bundles")
    monkeypatch.setenv("DEPLOY_ENV", "production")

    result = await views.get_client_bundle_signed_url(
        _FakeRequest("assistant-key"),
        assistant_id=1406,
        binding_id=None,
    )

    assert result["client_name"] == "unify_company"
    assert result["bundle_sha"] == "abc123"
    assert result["sha256"] == "deadbeef"
    assert result["signed_url"].startswith("https://signed.example/")


@pytest.mark.asyncio
async def test_client_bundle_rejects_mismatched_key(monkeypatch):
    async def _verify(*, api_key, assistant_id):
        raise HTTPException(
            status_code=401,
            detail="API key does not match the requested assistant.",
        )

    monkeypatch.setattr(views, "verify_assistant_identity_from_key", _verify)

    with pytest.raises(HTTPException) as exc:
        await views.get_client_bundle_signed_url(
            _FakeRequest("wrong-key"),
            assistant_id=1406,
        )
    assert exc.value.status_code == 401
