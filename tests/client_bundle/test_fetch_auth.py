"""Tests that the pod fetches its bundle with its own UNIFY_KEY, not admin key."""

from __future__ import annotations


from unify_deploy.client_bundle import fetch


def test_auth_headers_use_unify_key(monkeypatch):
    monkeypatch.setenv("UNIFY_KEY", "assistant-scoped-key")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "platform-admin-key")

    headers = fetch._auth_headers()

    assert headers == {"Authorization": "Bearer assistant-scoped-key"}
    # The platform admin key must never be used for bundle fetch.
    assert "platform-admin-key" not in headers.get("Authorization", "")
    # Server verifies UNIFY_KEY against the Orchestra assistant record
    # (no live AssistantSession required), so offline jobs can bootstrap.


def test_auth_headers_empty_without_unify_key(monkeypatch):
    monkeypatch.delenv("UNIFY_KEY", raising=False)
    assert fetch._auth_headers() == {}


def test_bundle_api_url_sends_assistant_and_binding(monkeypatch):
    monkeypatch.setenv("UNIFY_COMMS_URL", "http://comms:8080")

    url = fetch._bundle_api_url(assistant_id=7367, binding_id="bind-1")

    assert url.startswith("http://comms:8080/infra/client-bundle?")
    assert "assistant_id=7367" in url
    assert "binding_id=bind-1" in url
    # org_id is never sent by the client; the server derives it from the
    # verified session identity.
    assert "org_id" not in url


def test_fetch_bundle_metadata_uses_unify_key(monkeypatch):
    monkeypatch.setenv("UNIFY_COMMS_URL", "http://comms:8080")
    monkeypatch.setenv("UNIFY_KEY", "assistant-scoped-key")

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"signed_url": "https://x", "sha256": "abc"}

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(fetch.httpx, "get", _fake_get)

    out = fetch.fetch_bundle_metadata(assistant_id=7367, binding_id="bind-1")

    assert out["sha256"] == "abc"
    assert captured["headers"] == {"Authorization": "Bearer assistant-scoped-key"}
    assert "assistant_id=7367" in captured["url"]
