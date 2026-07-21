"""Contract tests for the ``POST /meet/workspace-events`` bridge.

Google delivers Meet Workspace Events only through Cloud Pub/Sub, formatted as
CloudEvents. These tests drive the real FastAPI endpoint and only stub the two
genuine external boundaries — the Google OIDC verification (a cert fetch) and
the outbound HTTP POST to Orchestra — so the CloudEvent mapping, Unify HMAC
signing, and ack policy all run as production code.
"""

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
os.environ.setdefault("NATIVE_GOOGLE_WEBHOOK_SECRET", "test-native-google-secret")

SECRET = "test-native-google-secret"
SA_EMAIL = "service-account@example.iam.gserviceaccount.com"
ORCHESTRA_URL = "http://orchestra.test/v0"
INGRESS_URL = f"{ORCHESTRA_URL}/webhooks/integrations/native_google"

SUBSCRIPTION_NAME = "subscriptions/AbCdEf123456"
CE_ID = "conferenceRecords/CR1/transcripts/T1/event-1"
CE_TYPE = "google.workspace.meet.transcript.v2.fileGenerated"


@pytest.fixture(scope="module")
def app_module():
    from adapters import main

    return main


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    test_client = TestClient(app_module.app)
    test_client.headers["Authorization"] = "Bearer push-oidc-token"
    yield test_client


def _push_envelope(
    *,
    ce_id: str = CE_ID,
    ce_source: str | None = f"//workspaceevents.googleapis.com/{SUBSCRIPTION_NAME}",
    ce_type: str | None = CE_TYPE,
) -> dict:
    """Build a CloudEvents binary-mode Meet transcript Pub/Sub push."""

    resource = {"transcript": {"name": "conferenceRecords/CR1/transcripts/T1"}}
    attributes = {
        "ce-id": ce_id,
        "ce-specversion": "1.0",
        "ce-time": "2026-07-21T12:00:00Z",
        "ce-subject": "//meet.googleapis.com/conferenceRecords/CR1",
        "ce-datacontenttype": "application/json",
    }
    if ce_source is not None:
        attributes["ce-source"] = ce_source
    if ce_type is not None:
        attributes["ce-type"] = ce_type
    return {
        "message": {
            "attributes": attributes,
            "data": base64.b64encode(json.dumps(resource).encode()).decode(),
            "messageId": "pubsub-message-1",
        },
        "subscription": "projects/p/subscriptions/meet-workspace-events-push",
    }


def _recompute_signature(headers: dict, raw_body: bytes) -> str:
    signed = f"{headers['x-unify-webhook-id']}.{headers['x-unify-webhook-timestamp']}."
    digest = hmac.new(
        SECRET.encode("utf-8"),
        signed.encode("utf-8") + raw_body,
        hashlib.sha256,
    ).digest()
    return f"v1,{base64.b64encode(digest).decode()}"


def test_bridge_forwards_signed_native_google_delivery_and_acks_on_2xx(
    app_module,
    client,
):
    """A valid Meet push is mapped, signed, and acked once Orchestra accepts."""

    orchestra_response = MagicMock(status_code=200, text='{"status":"accepted"}')
    with (
        patch("adapters.main.SETTINGS.native_google_webhook_secret", SECRET),
        patch("adapters.main.SETTINGS.meet_push_auth_service_account", SA_EMAIL),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        ),
        patch(
            "adapters.meet_bridge.requests.post",
            return_value=orchestra_response,
        ) as mock_post,
    ):
        response = client.post("/meet/workspace-events", json=_push_envelope())

    assert response.status_code == 200
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args

    posted_url = kwargs.get("url", args[0] if args else None)
    assert posted_url == INGRESS_URL

    raw_body = kwargs["data"]
    body = json.loads(raw_body)
    assert body["event_id"] == CE_ID
    assert body["provider_trigger_slug"] == CE_TYPE
    assert body["external_trigger_id"] == SUBSCRIPTION_NAME
    assert body["data"]["event_id"] == CE_ID
    assert body["data"]["transcript"]["name"].startswith("conferenceRecords/")

    headers = {k.lower(): v for k, v in kwargs["headers"].items()}
    assert headers["x-unify-webhook-signature"] == _recompute_signature(
        headers,
        raw_body,
    )


def test_bridge_returns_503_when_orchestra_is_not_durable(client):
    """A non-2xx from Orchestra must not ack, so Pub/Sub retries."""

    orchestra_response = MagicMock(status_code=500, text="boom")
    with (
        patch("adapters.main.SETTINGS.native_google_webhook_secret", SECRET),
        patch("adapters.main.SETTINGS.meet_push_auth_service_account", SA_EMAIL),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        ),
        patch(
            "adapters.meet_bridge.requests.post",
            return_value=orchestra_response,
        ),
    ):
        response = client.post("/meet/workspace-events", json=_push_envelope())

    assert response.status_code == 503


@pytest.mark.parametrize(
    "scenario",
    [
        "unauthenticated",
        "forged_issuer",
        "missing_event_id",
        "missing_subscription",
        "secret_unset",
    ],
)
def test_bridge_rejects_bad_pushes_without_contacting_orchestra(client, scenario):
    """Unauthenticated or unmappable pushes are rejected and wake no task."""

    from google.auth import exceptions as google_auth_exceptions

    if scenario == "unauthenticated":
        expected_status = 401
        envelope = _push_envelope()
        oidc = patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            side_effect=ValueError("bad token"),
        )
        secret_value = SECRET
    elif scenario == "forged_issuer":
        # A non-Google issuer raises GoogleAuthError, not ValueError; it must
        # still be rejected as 401 rather than surfacing a 500.
        expected_status = 401
        envelope = _push_envelope()
        oidc = patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            side_effect=google_auth_exceptions.GoogleAuthError("wrong issuer"),
        )
        secret_value = SECRET
    elif scenario == "missing_event_id":
        expected_status = 400
        envelope = _push_envelope(ce_id="")
        oidc = patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = SECRET
    elif scenario == "missing_subscription":
        expected_status = 400
        envelope = _push_envelope(ce_source=None)
        oidc = patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = SECRET
    else:  # secret_unset — fail closed before OIDC/mapping so Pub/Sub retries
        expected_status = 503
        envelope = _push_envelope()
        oidc = patch(
            "adapters.meet_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = ""

    with (
        patch("adapters.main.SETTINGS.native_google_webhook_secret", secret_value),
        patch("adapters.main.SETTINGS.meet_push_auth_service_account", SA_EMAIL),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        oidc,
        patch("adapters.meet_bridge.requests.post") as mock_post,
    ):
        response = client.post("/meet/workspace-events", json=envelope)

    assert response.status_code == expected_status
    mock_post.assert_not_called()


def test_duplicate_redeliveries_map_to_a_stable_event_id_and_body():
    """Retry-stable identity: the same CloudEvent maps to the same delivery."""

    from adapters import meet_bridge

    envelope = _push_envelope()
    first = meet_bridge.map_pubsub_push_to_delivery(envelope)
    second = meet_bridge.map_pubsub_push_to_delivery(envelope)

    assert first.event_id == CE_ID == second.event_id
    assert first.external_trigger_id == SUBSCRIPTION_NAME == second.external_trigger_id
    assert first.payload == second.payload
