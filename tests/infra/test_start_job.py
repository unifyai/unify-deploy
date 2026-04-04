"""
Focused tests for POST /infra/job/start session reuse behavior.
"""

from types import SimpleNamespace
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
    observed_activation_id: str | None = None,
    secret_name: str = "assistant-session-bootstrap-assistant-123",
    user_id: str = "stale-user",
    medium: str = "email",
    desktop_mode: str = "ubuntu",
    desktop_required: bool = True,
    binding: dict | None = None,
) -> dict:
    return {
        "metadata": {"name": "assistantsession-assistant-123"},
        "spec": {
            "activationId": activation_id,
            "startupSecretRef": secret_name,
            "userId": user_id,
            "medium": medium,
            "desiredState": "Running",
            "desktop": {
                "mode": desktop_mode,
                "required": desktop_required,
            },
        },
        "status": {
            "phase": phase,
            "observedActivationId": (
                observed_activation_id
                if observed_activation_id is not None
                else activation_id
            ),
            "binding": binding or {},
        },
    }


def test_start_job_refreshes_bootstrap_secret_and_session_spec_for_reused_pending_session(
    client,
):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session()
    refreshed_secret_name = "assistant-session-bootstrap-assistant-123"

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value=refreshed_secret_name,
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
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

    mock_create_or_update_assistant_session.assert_called_once()
    session_args = mock_create_or_update_assistant_session.call_args.args
    assert session_args[0] is custom_api
    assert session_args[1] == SETTINGS.default_namespace
    assert session_args[2] == "assistant-123"
    refreshed_spec = session_args[3]
    assert refreshed_spec["activationId"] == existing_session["spec"]["activationId"]
    assert refreshed_spec["userId"] == "user-123"
    assert refreshed_spec["medium"] == "phone"
    assert refreshed_spec["desiredState"] == "Running"
    assert refreshed_spec["desktop"] == {"mode": "ubuntu", "required": True}
    assert refreshed_spec["startupSecretRef"] == refreshed_secret_name
    assert refreshed_spec["requestedAt"]


def test_start_job_reused_pending_session_picks_up_changed_desktop_mode(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        desktop_mode="ubuntu",
        desktop_required=True,
    )

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(desktop_mode="macos"),
        )

    assert response.status_code == 200
    refreshed_spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert refreshed_spec["activationId"] == existing_session["spec"]["activationId"]
    assert refreshed_spec["desktop"] == {"mode": "macos", "required": False}


def test_start_job_reuses_inflight_restart_activation_for_terminal_session(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        phase="Succeeded",
        activation_id="activation-restart-pending",
        observed_activation_id="activation-old-terminal",
    )

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    assert response.json()["activation_id"] == "activation-restart-pending"
    refreshed_spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert refreshed_spec["activationId"] == "activation-restart-pending"


def test_start_job_reuses_activation_while_release_is_draining(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        phase="Releasing",
        activation_id="activation-draining",
        binding={
            "id": "binding-1",
            "vmRef": {
                "name": "unity-pool-ubuntu-1",
                "hostname": "vm-1.vm.unify.ai",
            },
        },
    )

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    assert response.json()["activation_id"] == "activation-draining"
    refreshed_spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert refreshed_spec["activationId"] == "activation-draining"


def test_start_job_mints_new_activation_after_released_session_cleanup(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        phase="Released",
        activation_id="activation-old",
    )

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ) as mock_create_or_update_assistant_session,
        patch(
            "communication.infra.views.uuid.uuid4",
            return_value=SimpleNamespace(hex="activation-new"),
        ),
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    assert response.json()["activation_id"] == "activation-new"
    refreshed_spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert refreshed_spec["activationId"] == "activation-new"
