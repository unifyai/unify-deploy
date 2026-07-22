"""Contract tests for ``POST /microsoft/native-triggers``.

Graph delivers change/lifecycle notifications over HTTPS. These tests drive the
real FastAPI endpoint and only stub the Orchestra HTTP boundary so mapping,
clientState verification, Unify HMAC signing, and ack policy run as production
code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("GCP_SA_KEY", '{"type": "service_account", "project_id": "test"}')
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("ORCHESTRA_URL", "http://orchestra.test/v0")
os.environ.setdefault("ORCHESTRA_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("NATIVE_MICROSOFT_WEBHOOK_SECRET", "test-native-microsoft-secret")

SECRET = "test-native-microsoft-secret"
ORCHESTRA_URL = "http://orchestra.test/v0"
INGRESS_URL = f"{ORCHESTRA_URL}/webhooks/integrations/native_microsoft"
SUBSCRIPTION_ID = "7f105c7d-2dc5-4530-97cd-4e7ae6534c07"


@pytest.fixture(scope="module")
def app_module():
    from adapters import main

    return main


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _notification(
    *,
    change_type: str = "created",
    resource: str = "me/messages",
    resource_id: str = "AAMkAGI2",
    client_state: str = SECRET,
    lifecycle_event: str | None = None,
    notification_id: str | None = "notif-stable-1",
) -> dict:
    item: dict = {
        "subscriptionId": SUBSCRIPTION_ID,
        "clientState": client_state,
        "tenantId": "tenant-1",
    }
    if notification_id is not None:
        item["id"] = notification_id
    if lifecycle_event:
        item["lifecycleEvent"] = lifecycle_event
    else:
        item["changeType"] = change_type
        item["resource"] = resource
        item["resourceData"] = {
            "@odata.type": "#Microsoft.Graph.Message",
            "id": resource_id,
        }
    return {"value": [item]}


def _recompute_signature(headers: dict, raw_body: bytes) -> str:
    signed = f"{headers['x-unify-webhook-id']}.{headers['x-unify-webhook-timestamp']}."
    digest = hmac.new(
        SECRET.encode("utf-8"),
        signed.encode("utf-8") + raw_body,
        hashlib.sha256,
    ).digest()
    return f"v1,{base64.b64encode(digest).decode()}"


def test_bridge_echoes_validation_token(client) -> None:
    response = client.post(
        "/microsoft/native-triggers",
        params={"validationToken": "validate-me-123"},
    )
    assert response.status_code == 200
    assert response.text == "validate-me-123"
    assert response.headers["content-type"].startswith("text/plain")


def test_bridge_forwards_signed_native_microsoft_change_notification(client) -> None:
    orchestra_response = MagicMock()
    orchestra_response.status_code = 200

    with (
        patch("adapters.main.SETTINGS.native_microsoft_webhook_secret", SECRET),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.microsoft_graph_bridge.requests.post",
            return_value=orchestra_response,
        ) as post_mock,
    ):
        response = client.post(
            "/microsoft/native-triggers",
            json=_notification(),
        )

    assert response.status_code == 202, response.text
    assert post_mock.call_count == 1
    args, kwargs = post_mock.call_args
    assert args[0] == INGRESS_URL
    raw_body = kwargs["data"]
    headers = kwargs["headers"]
    body = json.loads(raw_body)
    assert body["external_trigger_id"] == SUBSCRIPTION_ID
    assert body["provider_trigger_slug"] == "microsoft.graph.mailMessage.created"
    assert body["event_id"] == "notif-stable-1"
    assert headers["x-unify-webhook-signature"] == _recompute_signature(
        headers,
        raw_body,
    )


def test_bridge_forwards_lifecycle_notification_after_graph_ack(
    client,
) -> None:
    orchestra_response = MagicMock()
    orchestra_response.status_code = 503

    with (
        patch("adapters.main.SETTINGS.native_microsoft_webhook_secret", SECRET),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.microsoft_graph_bridge.requests.post",
            return_value=orchestra_response,
        ) as post_mock,
    ):
        response = client.post(
            "/microsoft/native-triggers",
            json=_notification(lifecycle_event="subscriptionRemoved"),
        )

    # Graph is acked before Orchestra forward; Orchestra failures are logged
    # in the background task and do not change the Graph response.
    assert response.status_code == 202
    assert post_mock.call_count == 1
    body = json.loads(post_mock.call_args.kwargs["data"])
    assert body["lifecycle_event"] == "subscriptionRemoved"
    assert body["provider_trigger_slug"] == "microsoft.graph.lifecycle"


def test_bridge_rejects_bad_client_state_without_contacting_orchestra(client) -> None:
    with (
        patch("adapters.main.SETTINGS.native_microsoft_webhook_secret", SECRET),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch("adapters.microsoft_graph_bridge.requests.post") as post_mock,
    ):
        response = client.post(
            "/microsoft/native-triggers",
            json=_notification(client_state="wrong-secret"),
        )

    assert response.status_code == 401
    post_mock.assert_not_called()


def test_duplicate_redeliveries_map_to_stable_event_id() -> None:
    from adapters.microsoft_graph_bridge import map_graph_notification_to_delivery

    first = map_graph_notification_to_delivery(_notification()["value"][0])
    second = map_graph_notification_to_delivery(_notification()["value"][0])
    assert first.event_id == second.event_id == "notif-stable-1"
    assert first.external_trigger_id == SUBSCRIPTION_ID


def test_fallback_event_id_includes_etag_for_distinct_updates() -> None:
    from adapters.microsoft_graph_bridge import map_graph_notification_to_delivery

    base = _notification(notification_id=None)["value"][0]
    base["resourceData"]["@odata.etag"] = 'W/"1"'
    first = map_graph_notification_to_delivery(base)
    base["resourceData"]["@odata.etag"] = 'W/"2"'
    second = map_graph_notification_to_delivery(base)
    assert first.event_id != second.event_id
    assert 'W/"1"' in first.event_id
