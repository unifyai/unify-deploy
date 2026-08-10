"""Unit tests for ``_resolve_ms_refresh_credentials`` in ``adapters/main.py``.

Covers per-source dispatch for the two flows that still mint Microsoft
tokens (``byod`` and ``enterprise``) plus the safety net that prevents
the retired ``unify_ropc`` source from silently re-cycling stale
credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

os.environ.setdefault("OUTLOOK_WEBHOOK_SECRET", "test-outlook-secret")
os.environ.setdefault("OAUTH_STATE_SIGNING_KEY", "test-oauth-signing-key")


@pytest.fixture
def ms_env(monkeypatch):
    """Populate the Azure-app env vars the BYOD branch relies on."""
    monkeypatch.setenv("MS365_BYOD_CLIENT_SECRET", "byod-secret")

    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "ms365_byod_client_id", "byod-client-id")
    return SETTINGS


def test_byod_honors_stored_granted_scopes(ms_env):
    """BYOD refresh keeps honoring the user-consented scope string."""
    from adapters.main import _resolve_ms_refresh_credentials

    stored = "https://graph.microsoft.com/Mail.Read offline_access"
    assistant = {
        "email": "user@example.com",
        "secrets": {
            "MICROSOFT_TOKEN_SOURCE": "byod",
            "MICROSOFT_GRANTED_SCOPES": stored,
        },
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    tenant_id, client_id, client_secret, scope, source = creds
    assert source == "byod"
    assert tenant_id == "common"
    assert client_id == "byod-client-id"
    assert client_secret == "byod-secret"
    assert scope == stored


def test_byod_falls_back_to_default_scope(ms_env):
    """Legacy BYOD row with no stored scopes falls back to ``.default``."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@example.com",
        "secrets": {"MICROSOFT_TOKEN_SOURCE": "byod"},
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    assert creds[3] == "https://graph.microsoft.com/.default offline_access"


def test_byod_is_default_when_source_unset(ms_env):
    """Rows with no explicit source and no AZURE_* secrets default to BYOD."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@example.com",
        "secrets": {},
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    assert creds[4] == "byod"


def test_enterprise_always_uses_default_scope(ms_env):
    """Enterprise uses ``.default`` and per-assistant Azure creds regardless of stored scopes."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@enterprise.example.com",
        "secrets": {
            "AZURE_TENANT_ID": "ent-tenant",
            "AZURE_CLIENT_ID": "ent-client",
            "AZURE_CLIENT_SECRET": "ent-secret",
            "MICROSOFT_GRANTED_SCOPES": (
                "https://graph.microsoft.com/Mail.Read offline_access"
            ),
        },
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    tenant_id, client_id, client_secret, scope, source = creds
    assert source == "enterprise"
    assert (tenant_id, client_id, client_secret) == (
        "ent-tenant",
        "ent-client",
        "ent-secret",
    )
    assert scope == "https://graph.microsoft.com/.default offline_access"


def test_enterprise_inferred_from_azure_secrets(ms_env):
    """AZURE_* secrets without explicit source are classified as enterprise."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@enterprise.example.com",
        "secrets": {
            "AZURE_TENANT_ID": "ent-tenant",
            "AZURE_CLIENT_ID": "ent-client",
            "AZURE_CLIENT_SECRET": "ent-secret",
        },
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    assert creds[4] == "enterprise"


def test_unify_ropc_source_returns_none(ms_env):
    """The retired ``unify_ropc`` source is rejected to avoid re-cycling stale creds.

    The platform mailboxes that minted these tokens were torn down with
    the wider @unify.ai email feature; any straggler row stamped with
    this source must surface as a failure rather than silently calling
    Microsoft with retired admin-app credentials.
    """
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@tenant.onmicrosoft.com",
        "secrets": {
            "MICROSOFT_TOKEN_SOURCE": "unify_ropc",
            "MICROSOFT_REFRESH_TOKEN": "stale-refresh-token",
        },
    }

    assert _resolve_ms_refresh_credentials(assistant) is None


def test_byod_missing_env_returns_none(monkeypatch):
    """Missing BYOD env vars must surface as ``None``, not an empty-string call."""
    monkeypatch.delenv("MS365_BYOD_CLIENT_SECRET", raising=False)

    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "ms365_byod_client_id", "")

    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@example.com",
        "secrets": {"MICROSOFT_TOKEN_SOURCE": "byod"},
    }

    assert _resolve_ms_refresh_credentials(assistant) is None


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_store_refreshed_oauth_secrets_uses_post_fallback(monkeypatch):
    """Refresh persistence treats PUT 404 + POST 201 as a successful upsert."""
    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "orchestra_url", "https://orchestra.test")

    calls: list[tuple[str, str, dict]] = []

    def fake_put(url, json, headers):
        calls.append(("PUT", url, json))
        return _Resp(404, "missing")

    def fake_post(url, json, headers):
        calls.append(("POST", url, json))
        return _Resp(201, "created")

    from adapters import main

    monkeypatch.setattr(main.requests, "put", fake_put)
    monkeypatch.setattr(main.requests, "post", fake_post)

    ok, error = main._store_refreshed_oauth_secrets(
        provider="fake",
        assistant_id="123",
        assistant_email="user@example.com",
        api_key="assistant-key",
        secrets_to_store={"FAKE_ACCESS_TOKEN": "token"},
    )

    assert ok is True
    assert error is None
    assert calls == [
        (
            "PUT",
            "https://orchestra.test/assistant/123/secret/FAKE_ACCESS_TOKEN",
            {"secret_value": "token"},
        ),
        (
            "POST",
            "https://orchestra.test/assistant/123/secret",
            {"secret_name": "FAKE_ACCESS_TOKEN", "secret_value": "token"},
        ),
    ]


def test_store_refreshed_oauth_secrets_fails_on_persistence_error(monkeypatch):
    """A refresh job must not be counted refreshed when credential storage fails."""
    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "orchestra_url", "https://orchestra.test")

    from adapters import main

    monkeypatch.setattr(
        main.requests,
        "put",
        lambda *args, **kwargs: _Resp(500, "nope"),
    )

    ok, error = main._store_refreshed_oauth_secrets(
        provider="fake",
        assistant_id="123",
        assistant_email="user@example.com",
        api_key="assistant-key",
        secrets_to_store={"FAKE_ACCESS_TOKEN": "token"},
    )

    assert ok is False
    assert "FAKE_ACCESS_TOKEN" in (error or "")
    assert "token" not in (error or "")
