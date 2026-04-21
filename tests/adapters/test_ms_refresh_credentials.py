"""Unit tests for ``_resolve_ms_refresh_credentials`` in ``adapters/main.py``.

Covers per-source scope dispatch, with particular emphasis on the
``unify_ropc`` branch which must always use the freshly-computed
``email + teams`` bundle from ``common/scopes.py`` regardless of what
was stamped in ``MICROSOFT_GRANTED_SCOPES`` at provisioning time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

os.environ.setdefault("OUTLOOK_WEBHOOK_SECRET", "test-outlook-secret")
os.environ.setdefault("OAUTH_STATE_SIGNING_KEY", "test-oauth-signing-key")


@pytest.fixture
def ms_env(monkeypatch):
    """Populate the Azure-app env vars the three branches rely on."""
    monkeypatch.setenv("MS365_ADMIN_CLIENT_SECRET", "admin-secret")
    monkeypatch.setenv("MS365_BYOD_CLIENT_SECRET", "byod-secret")

    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "ms365_admin_tenant_id", "admin-tenant-id")
    monkeypatch.setattr(SETTINGS, "ms365_admin_client_id", "admin-client-id")
    monkeypatch.setattr(SETTINGS, "ms365_byod_client_id", "byod-client-id")
    monkeypatch.setattr(SETTINGS, "ms365_email_domain", "tenant.onmicrosoft.com")
    return SETTINGS


def _expected_ropc_scope() -> str:
    from common.scopes import build_scope_string

    return build_scope_string("microsoft", ["email", "teams"])


def test_unify_ropc_ignores_stale_granted_scopes(ms_env):
    """Stale ``MICROSOFT_GRANTED_SCOPES`` must not leak into the ropc refresh."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@tenant.onmicrosoft.com",
        "secrets": {
            "MICROSOFT_TOKEN_SOURCE": "unify_ropc",
            "MICROSOFT_GRANTED_SCOPES": (
                "https://graph.microsoft.com/Mail.Read offline_access"
            ),
        },
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    tenant_id, client_id, client_secret, scope, source = creds
    assert source == "unify_ropc"
    assert tenant_id == "admin-tenant-id"
    assert client_id == "admin-client-id"
    assert client_secret == "admin-secret"
    assert scope == _expected_ropc_scope()
    # Sanity: the stale single-scope value must not have survived.
    assert "Mail.Send" in scope
    assert "ChannelMessage.Read.All" in scope


def test_unify_ropc_no_stored_scopes(ms_env):
    """Missing ``MICROSOFT_GRANTED_SCOPES`` still yields the full bundle."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@tenant.onmicrosoft.com",
        "secrets": {"MICROSOFT_TOKEN_SOURCE": "unify_ropc"},
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    assert creds[3] == _expected_ropc_scope()


def test_unify_ropc_classified_by_email_domain(ms_env):
    """Mailboxes on ``ms365_email_domain`` fall into ropc without explicit source."""
    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@tenant.onmicrosoft.com",
        "secrets": {},
    }

    creds = _resolve_ms_refresh_credentials(assistant)

    assert creds is not None
    assert creds[4] == "unify_ropc"
    assert creds[3] == _expected_ropc_scope()


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


def test_unify_ropc_missing_admin_env_returns_none(monkeypatch):
    """Missing admin-app env vars must surface as ``None``, not an empty-string call."""
    monkeypatch.delenv("MS365_ADMIN_CLIENT_SECRET", raising=False)

    from common.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "ms365_admin_tenant_id", "")
    monkeypatch.setattr(SETTINGS, "ms365_admin_client_id", "")
    monkeypatch.setattr(SETTINGS, "ms365_email_domain", "tenant.onmicrosoft.com")

    from adapters.main import _resolve_ms_refresh_credentials

    assistant = {
        "email": "user@tenant.onmicrosoft.com",
        "secrets": {"MICROSOFT_TOKEN_SOURCE": "unify_ropc"},
    }

    assert _resolve_ms_refresh_credentials(assistant) is None
