from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from droid_deploy.utils import orchestra_client


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict:
        return self._body


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        orchestra_client.SETTINGS,
        "ORCHESTRA_ADMIN_KEY",
        SecretStr("admin-token"),
    )
    monkeypatch.setattr(
        orchestra_client.SETTINGS,
        "ORCHESTRA_URL",
        "https://api.unify.ai/v0",
    )


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        (
            "https://api.unify.ai/v0",
            "/admin/assistant/123",
            "https://api.unify.ai/v0/admin/assistant/123",
        ),
        (
            "https://api.unify.ai/v0/",
            "/admin/assistant/123",
            "https://api.unify.ai/v0/admin/assistant/123",
        ),
        (
            "http://localhost:8000",
            "/admin/assistant/123",
            "http://localhost:8000/v0/admin/assistant/123",
        ),
        (
            "https://api.unify.ai/v0",
            "/v0/admin/assistant/123",
            "https://api.unify.ai/v0/admin/assistant/123",
        ),
        (
            "https://api.unify.ai",
            "admin/assistant/123",
            "https://api.unify.ai/v0/admin/assistant/123",
        ),
    ],
)
def test_build_url_normalizes_versioned_and_unversioned_inputs(
    monkeypatch,
    base_url: str,
    path: str,
    expected: str,
):
    monkeypatch.setattr(orchestra_client.SETTINGS, "ORCHESTRA_URL", base_url)

    assert orchestra_client._build_url(path) == expected


def test_patch_json_sends_admin_bearer_and_body(monkeypatch):
    captured: dict = {}

    def fake_patch(url, *, json, headers, timeout):
        captured.update(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            },
        )
        return _FakeResponse(200, {"status": "ok"})

    monkeypatch.setattr(orchestra_client.httpx, "patch", fake_patch)

    body = {"console_config": {"version": "1"}}
    response = orchestra_client.patch_json("/admin/assistant/123", body)

    assert response == {"status": "ok"}
    assert captured == {
        "url": "https://api.unify.ai/v0/admin/assistant/123",
        "json": body,
        "headers": {"Authorization": "Bearer admin-token"},
        "timeout": 10.0,
    }


def test_patch_json_raises_for_http_error(monkeypatch):
    def fake_patch(url, *, json, headers, timeout):
        return _FakeResponse(500, text="server exploded")

    monkeypatch.setattr(orchestra_client.httpx, "patch", fake_patch)

    with pytest.raises(orchestra_client.OrchestraClientError) as exc_info:
        orchestra_client.patch_json("/admin/assistant/123", {})

    assert exc_info.value.status_code == 500
    assert "server exploded" in exc_info.value.detail


def test_patch_json_raises_for_transport_error(monkeypatch):
    def fake_patch(url, *, json, headers, timeout):
        raise httpx.TransportError("network down")

    monkeypatch.setattr(orchestra_client.httpx, "patch", fake_patch)

    with pytest.raises(orchestra_client.OrchestraClientError) as exc_info:
        orchestra_client.patch_json("/admin/assistant/123", {})

    assert exc_info.value.status_code == 0
    assert "network down" in exc_info.value.detail
