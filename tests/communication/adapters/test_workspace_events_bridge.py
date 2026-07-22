"""Contract tests for the ``POST /workspace-events`` bridge.

Google delivers Workspace Events only through Cloud Pub/Sub, formatted as
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
MEET_CE_ID = "conferenceRecords/CR1/transcripts/T1/event-1"
MEET_CE_TYPE = "google.workspace.meet.transcript.v2.fileGenerated"
DRIVE_CE_ID = "files/FILE1/event-1"
DRIVE_CE_TYPE = "google.workspace.drive.file.v3.created"
CHAT_CE_ID = "spaces/SPACE1/messages/MSG1/event-1"
CHAT_CE_TYPE = "google.workspace.chat.message.v1.created"


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
    ce_id: str = MEET_CE_ID,
    ce_source: str | None = f"//workspaceevents.googleapis.com/{SUBSCRIPTION_NAME}",
    ce_type: str | None = MEET_CE_TYPE,
    ce_subject: str = "//meet.googleapis.com/conferenceRecords/CR1",
    resource: dict | None = None,
) -> dict:
    """Build a CloudEvents binary-mode Workspace Events Pub/Sub push."""

    if resource is None:
        resource = {"transcript": {"name": "conferenceRecords/CR1/transcripts/T1"}}
    attributes = {
        "ce-id": ce_id,
        "ce-specversion": "1.0",
        "ce-time": "2026-07-21T12:00:00Z",
        "ce-subject": ce_subject,
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
        "subscription": "projects/p/subscriptions/workspace-events-push",
    }


def _recompute_signature(headers: dict, raw_body: bytes) -> str:
    signed = f"{headers['x-unify-webhook-id']}.{headers['x-unify-webhook-timestamp']}."
    digest = hmac.new(
        SECRET.encode("utf-8"),
        signed.encode("utf-8") + raw_body,
        hashlib.sha256,
    ).digest()
    return f"v1,{base64.b64encode(digest).decode()}"


@pytest.mark.parametrize(
    ("ce_id", "ce_type", "ce_subject", "resource", "expected_slug"),
    [
        (
            MEET_CE_ID,
            MEET_CE_TYPE,
            "//meet.googleapis.com/conferenceRecords/CR1",
            {"transcript": {"name": "conferenceRecords/CR1/transcripts/T1"}},
            MEET_CE_TYPE,
        ),
        (
            DRIVE_CE_ID,
            DRIVE_CE_TYPE,
            "//drive.googleapis.com/files/FILE1",
            {"file": {"name": "files/FILE1"}},
            DRIVE_CE_TYPE,
        ),
        (
            CHAT_CE_ID,
            CHAT_CE_TYPE,
            "//chat.googleapis.com/spaces/SPACE1",
            {"message": {"name": "spaces/SPACE1/messages/MSG1"}},
            CHAT_CE_TYPE,
        ),
    ],
)
def test_bridge_forwards_signed_native_google_delivery_for_workspace_families(
    app_module,
    client,
    ce_id,
    ce_type,
    ce_subject,
    resource,
    expected_slug,
):
    """Meet/Drive/Chat ce-types map, sign, and ack once Orchestra accepts."""

    orchestra_response = MagicMock(status_code=200, text='{"status":"accepted"}')
    with (
        patch("adapters.main.SETTINGS.native_google_webhook_secret", SECRET),
        patch(
            "adapters.main.SETTINGS.workspace_events_push_auth_service_account",
            SA_EMAIL,
        ),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        ),
        patch(
            "adapters.workspace_events_bridge.requests.post",
            return_value=orchestra_response,
        ) as mock_post,
    ):
        response = client.post(
            "/workspace-events",
            json=_push_envelope(
                ce_id=ce_id,
                ce_type=ce_type,
                ce_subject=ce_subject,
                resource=resource,
            ),
        )

    assert response.status_code == 200
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args

    posted_url = kwargs.get("url", args[0] if args else None)
    assert posted_url == INGRESS_URL

    raw_body = kwargs["data"]
    body = json.loads(raw_body)
    assert body["event_id"] == ce_id
    assert body["provider_trigger_slug"] == expected_slug
    assert body["external_trigger_id"] == SUBSCRIPTION_NAME
    assert body["data"]["event_id"] == ce_id
    assert body["data"]["resource"] == ce_subject

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
        patch(
            "adapters.main.SETTINGS.workspace_events_push_auth_service_account",
            SA_EMAIL,
        ),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        ),
        patch(
            "adapters.workspace_events_bridge.requests.post",
            return_value=orchestra_response,
        ),
    ):
        response = client.post("/workspace-events", json=_push_envelope())

    assert response.status_code == 503


@pytest.mark.parametrize(
    "scenario",
    [
        "unauthenticated",
        "forged_issuer",
        "missing_event_id",
        "missing_subscription",
        "missing_ce_type",
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
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            side_effect=ValueError("bad token"),
        )
        secret_value = SECRET
    elif scenario == "forged_issuer":
        # A non-Google issuer raises GoogleAuthError, not ValueError; it must
        # still be rejected as 401 rather than surfacing a 500.
        expected_status = 401
        envelope = _push_envelope()
        oidc = patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            side_effect=google_auth_exceptions.GoogleAuthError("wrong issuer"),
        )
        secret_value = SECRET
    elif scenario == "missing_event_id":
        expected_status = 400
        envelope = _push_envelope(ce_id="")
        oidc = patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = SECRET
    elif scenario == "missing_subscription":
        expected_status = 400
        envelope = _push_envelope(ce_source=None)
        oidc = patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = SECRET
    elif scenario == "missing_ce_type":
        expected_status = 400
        envelope = _push_envelope(ce_type=None)
        oidc = patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = SECRET
    else:  # secret_unset — fail closed before OIDC/mapping so Pub/Sub retries
        expected_status = 503
        envelope = _push_envelope()
        oidc = patch(
            "adapters.workspace_events_bridge.google_id_token.verify_oauth2_token",
            return_value={"email": SA_EMAIL, "email_verified": True},
        )
        secret_value = ""

    with (
        patch("adapters.main.SETTINGS.native_google_webhook_secret", secret_value),
        patch(
            "adapters.main.SETTINGS.workspace_events_push_auth_service_account",
            SA_EMAIL,
        ),
        patch("adapters.main.SETTINGS.orchestra_url", ORCHESTRA_URL),
        oidc,
        patch("adapters.workspace_events_bridge.requests.post") as mock_post,
    ):
        response = client.post("/workspace-events", json=envelope)

    assert response.status_code == expected_status
    mock_post.assert_not_called()


def test_duplicate_redeliveries_map_to_a_stable_event_id_and_body():
    """Retry-stable identity: the same CloudEvent maps to the same delivery."""

    from adapters import workspace_events_bridge

    envelope = _push_envelope()
    first = workspace_events_bridge.map_pubsub_push_to_delivery(envelope)
    second = workspace_events_bridge.map_pubsub_push_to_delivery(envelope)

    assert first.event_id == MEET_CE_ID == second.event_id
    assert first.external_trigger_id == SUBSCRIPTION_NAME == second.external_trigger_id
    assert first.payload == second.payload
