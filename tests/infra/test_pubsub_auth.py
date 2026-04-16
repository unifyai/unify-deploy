"""Unit tests for integration-test Pub/Sub credential resolution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import PermissionDenied

from tests.infra.integration import conftest as integration_conftest
from tests.infra.integration import pubsub_auth

SERVICE_ACCOUNT_INFO = {
    "type": "service_account",
    "project_id": "gcp-project-runtime",
    "private_key_id": "1",
    "private_key": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n",
    "client_email": "service-account@example.iam.gserviceaccount.com",
    "client_id": "1",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def test_resolve_pubsub_credentials_prefers_inline_json(monkeypatch):
    """Inline service-account JSON should win over file creds and ADC."""

    created = MagicMock(name="inline-creds")
    monkeypatch.setenv("TEST_GCP_SA_KEY", json.dumps(SERVICE_ACCOUNT_INFO))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/should-not-be-used.json")
    monkeypatch.setattr(
        pubsub_auth.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes=None: created,
    )
    monkeypatch.setattr(
        pubsub_auth.service_account.Credentials,
        "from_service_account_file",
        lambda *args, **kwargs: pytest.fail("file credentials should not be used"),
    )
    monkeypatch.setattr(
        pubsub_auth,
        "google_auth_default",
        lambda *args, **kwargs: pytest.fail("ADC should not be used"),
    )

    resolved = pubsub_auth.resolve_pubsub_credentials()

    assert resolved.credentials is created
    assert resolved.source == "TEST_GCP_SA_KEY"
    assert resolved.principal == SERVICE_ACCOUNT_INFO["client_email"]
    assert resolved.project_id == SERVICE_ACCOUNT_INFO["project_id"]


def test_resolve_pubsub_credentials_uses_explicit_file_before_adc(
    monkeypatch,
    tmp_path,
):
    """An explicit credential file should be chosen before ambient ADC."""

    credential_path = tmp_path / "comm-sa.json"
    credential_path.write_text(json.dumps(SERVICE_ACCOUNT_INFO))
    created = MagicMock(name="file-creds")

    monkeypatch.setenv("TEST_GOOGLE_APPLICATION_CREDENTIALS", str(credential_path))
    monkeypatch.setattr(
        pubsub_auth.service_account.Credentials,
        "from_service_account_file",
        lambda *args, **kwargs: created,
    )
    monkeypatch.setattr(
        pubsub_auth,
        "google_auth_default",
        lambda *args, **kwargs: pytest.fail("ADC should not be used"),
    )

    resolved = pubsub_auth.resolve_pubsub_credentials()

    assert resolved.credentials is created
    assert resolved.source == "TEST_GOOGLE_APPLICATION_CREDENTIALS"
    assert resolved.source_detail == str(credential_path)
    assert resolved.principal == SERVICE_ACCOUNT_INFO["client_email"]
    assert resolved.project_id == SERVICE_ACCOUNT_INFO["project_id"]


def test_pull_outbound_messages_reports_resolved_identity_on_permission_denied(
    monkeypatch,
):
    """Permission errors should surface the chosen Pub/Sub principal clearly."""

    fake_client = MagicMock(name="subscriber-client")
    fake_client.subscription_path.return_value = "projects/gcp-project-runtime/subscriptions/unity-1808-staging-outbound-sub"
    fake_client.pull.side_effect = PermissionDenied("forbidden")
    monkeypatch.setattr(
        pubsub_auth.pubsub_v1,
        "SubscriberClient",
        lambda credentials=None: fake_client,
    )

    resolved = pubsub_auth.ResolvedPubSubCredentials(
        credentials=MagicMock(name="pubsub-creds"),
        source="TEST_GOOGLE_APPLICATION_CREDENTIALS",
        source_detail="/tmp/comm-sa.json",
        principal=SERVICE_ACCOUNT_INFO["client_email"],
        project_id=SERVICE_ACCOUNT_INFO["project_id"],
        credential_type="service_account",
    )
    subscriber = pubsub_auth.build_pubsub_subscriber_client(resolved)

    with pytest.raises(AssertionError) as exc_info:
        integration_conftest.pull_outbound_messages(subscriber, "1808")

    message = str(exc_info.value)
    assert "pubsub.subscriptions.consume" not in message
    assert SERVICE_ACCOUNT_INFO["client_email"] in message
    assert "TEST_GOOGLE_APPLICATION_CREDENTIALS" in message
    assert "unity-1808-staging-outbound-sub" in message
