"""The redirect resolves, records, and sends the visitor on.

The service exists because brain rewrote every outbound URL through
``r.unify.ai`` while nothing served that host, so every tracked link was dead.
These pin the parts that would make it silently wrong rather than broken.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# unisdk is installed in the deployed image, not in the test environment.
if "unisdk" not in sys.modules:  # pragma: no cover - import shim
    sys.modules["unisdk"] = types.SimpleNamespace(get_logs=None, create_logs=None)

from link_tracker import main as link_tracker  # noqa: E402

_SHORTLINK = {
    "short_id": "kACbJbb5",
    "canonical_url": "https://console.unify.ai/assistants?token=abc123",
    "contact_id": None,
    "campaign_id": 3637897,
    "channel": "email",
    "step": "reply-credit-grant",
    "utm_source": "email",
    "utm_medium": "reply-credit-grant",
    "utm_campaign": "smartlead-3637897",
    "utm_content": "anon",
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(link_tracker.app, follow_redirects=False)


def test_known_link_redirects_with_utms(client: TestClient) -> None:
    with (
        patch.object(link_tracker, "lookup_shortlink", return_value=dict(_SHORTLINK)),
        patch.object(link_tracker, "record_click") as record,
    ):
        response = client.get("/kACbJbb5")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://console.unify.ai/assistants?")
    assert "token=abc123" in location
    assert "utm_campaign=smartlead-3637897" in location
    assert response.headers["cache-control"] == "no-store"
    record.assert_called_once()


def test_unknown_link_is_a_404(client: TestClient) -> None:
    with patch.object(link_tracker, "lookup_shortlink", return_value=None):
        assert client.get("/nosuchid").status_code == 404


def test_a_failed_click_write_still_redirects(client: TestClient) -> None:
    """Attribution is worth less than the prospect reaching the page."""

    with (
        patch.object(link_tracker, "lookup_shortlink", return_value=dict(_SHORTLINK)),
        patch.object(
            link_tracker,
            "record_click",
            side_effect=RuntimeError("orchestra down"),
        ),
    ):
        response = client.get("/kACbJbb5")

    assert response.status_code == 302


def test_short_id_is_rejected_before_it_reaches_a_filter(client: TestClient) -> None:
    """The id is interpolated into an Orchestra filter expression."""

    with patch.object(link_tracker, "lookup_shortlink") as lookup:
        response = client.get('/" or short_id != "')

    assert response.status_code == 404
    lookup.assert_not_called()


def test_existing_query_params_are_never_overwritten() -> None:
    url = link_tracker.build_redirect_url(
        canonical_url="https://x.test/p?utm_source=mine&a=1",
        utm_source="email",
        utm_medium="step",
        utm_campaign="camp",
        utm_content="anon",
    )
    assert "utm_source=mine" in url
    assert "utm_source=email" not in url
    assert "a=1" in url
    assert "utm_medium=step" in url


def test_ip_is_dropped_when_no_salt_is_configured(monkeypatch) -> None:
    monkeypatch.delenv("LINK_TRACKER_IP_HASH_SALT", raising=False)
    assert link_tracker._hash_ip("1.2.3.4") is None


def test_ip_is_hashed_not_stored(monkeypatch) -> None:
    monkeypatch.setenv("LINK_TRACKER_IP_HASH_SALT", "pepper")
    hashed = link_tracker._hash_ip("1.2.3.4")
    assert hashed and "1.2.3.4" not in hashed


def test_click_rows_carry_a_timestamp() -> None:
    """A click with no time cannot be attributed to a campaign window.

    brain's model defaults this, but the default only applies when brain writes
    the row — this service posts entries straight to Orchestra.
    """

    stamp = link_tracker._utc_now()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp
