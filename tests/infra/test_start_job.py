"""
Focused tests for POST /infra/job/start session reuse behavior.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.settings import SETTINGS


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def _start_job_payload(**overrides) -> dict[str, str]:
    payload = {
        "api_key": "test-api-key",
        "medium": "phone",
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "assistant_first_name": "Updated",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Updated pending-session payload",
        "assistant_timezone": "UTC",
        "user_number": "+1234567890",
        "assistant_number": "+1987654321",
        "assistant_email": "assistant@example.com",
        "user_whatsapp_number": "+1234567890",
        "assistant_whatsapp_number": "+1987654321",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "ubuntu",
        "desktop_url": "",
        "user_desktop_mode": "",
        "user_desktop_filesys_sync": "false",
        "user_desktop_url": "",
        "demo_id": "",
        "team_ids": "[]",
        "org_id": "",
    }
    payload.update(overrides)
    return payload


def _existing_session(
    *,
    phase: str = "PendingVM",
    activation_id: str = "activation-existing",
    secret_name: str = "assistant-session-assistant-123-startup",
) -> dict:
    return {
        "metadata": {"name": "assistantsession-assistant-123"},
        "spec": {
            "activationId": activation_id,
            "startupSecretRef": secret_name,
        },
        "status": {
            "phase": phase,
        },
    }


def test_start_job_refreshes_bootstrap_secret_for_reused_pending_session(client):
    core_api = MagicMock()
    existing_session = _existing_session()

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value=existing_session["spec"]["startupSecretRef"],
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(
                assistant_about="Newest assistant bio",
                voice_id="voice-updated",
            ),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["activation_id"] == existing_session["spec"]["activationId"]
    assert body["phase"] == existing_session["status"]["phase"]

    mock_create_or_update_bootstrap_secret.assert_called_once()
    secret_args = mock_create_or_update_bootstrap_secret.call_args.args
    assert secret_args[0] is core_api
    assert secret_args[1] == SETTINGS.default_namespace
    assert secret_args[2] == "assistant-123"
    assert secret_args[3]["assistant_about"] == "Newest assistant bio"
    assert secret_args[3]["voice_id"] == "voice-updated"

    mock_create_or_update_assistant_session.assert_not_called()
