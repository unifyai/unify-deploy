"""Tests for membership bootstrap payload plumbing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_idle_pool_replenishment():
    with patch(
        "communication.infra.views.schedule_idle_job_pool_replenishment",
        return_value=True,
    ):
        yield


def _start_job_payload(**overrides) -> dict[str, str]:
    payload = {
        "api_key": "test-api-key",
        "medium": "phone",
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Test assistant",
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
        "user_desktops": "[]",
        "demo_id": "",
        "team_ids": "",
        "team_summaries": "",
        "self_contact_id": "42",
        "boss_contact_id": "43",
        "org_id": "",
    }
    payload.update(overrides)
    return payload


def _existing_session() -> dict:
    return {
        "metadata": {"name": "assistantsession-assistant-123"},
        "spec": {
            "activationId": "activation-existing",
            "startupSecretRef": "assistant-session-bootstrap-assistant-123",
            "userId": "user-123",
            "medium": "phone",
            "desiredState": "Running",
            "desktop": {"mode": "ubuntu", "required": True},
        },
        "status": {
            "phase": "PendingVM",
            "observedActivationId": "activation-existing",
            "binding": {},
        },
    }


def _post_start_job(client: TestClient, **payload_overrides):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session()

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": existing_session["metadata"],
            "spec": spec,
            "status": existing_session["status"],
        }

    patches = (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views._assistant_session_control_plane_ready",
            new_callable=AsyncMock,
            return_value=(True, None),
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
            return_value=existing_session["spec"]["startupSecretRef"],
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4] as bootstrap,
        patches[5],
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(**payload_overrides),
        )
    return response, bootstrap


def _bootstrap_payload(bootstrap: MagicMock) -> dict:
    core_api, namespace, assistant_id, activation_id, payload = bootstrap.call_args.args
    assert core_api is not None
    assert namespace
    assert assistant_id == "assistant-123"
    assert activation_id
    return payload


def test_form_json_string_parsed_to_list(client):
    response, bootstrap = _post_start_job(
        client,
        team_ids="[1, 2, 3]",
        team_summaries=(
            '[{"team_id": 1, "name": "Ops", '
            '"description": "Operations workspace for customer support."}]'
        ),
    )

    assert response.status_code == 200
    payload = _bootstrap_payload(bootstrap)
    assert payload["team_ids"] == [1, 2, 3]
    assert payload["team_summaries"] == [
        {
            "team_id": 1,
            "name": "Ops",
            "description": "Operations workspace for customer support.",
        },
    ]


def test_empty_form_means_empty_list(client):
    response, bootstrap = _post_start_job(client, team_ids="")

    assert response.status_code == 200
    payload = _bootstrap_payload(bootstrap)
    assert payload["team_ids"] == []
    assert payload["team_summaries"] == []


def test_contact_ids_required_in_bootstrap_payload(client):
    response, bootstrap = _post_start_job(client)

    assert response.status_code == 200
    payload = _bootstrap_payload(bootstrap)
    assert payload["self_contact_id"] == 42
    assert payload["boss_contact_id"] == 43


def test_contact_ids_override_in_bootstrap_payload(client):
    response, bootstrap = _post_start_job(
        client,
        self_contact_id="42",
        boss_contact_id="43",
    )

    assert response.status_code == 200
    payload = _bootstrap_payload(bootstrap)
    assert payload["self_contact_id"] == 42
    assert payload["boss_contact_id"] == 43


def test_missing_contact_ids_returns_422(client):
    payload = _start_job_payload()
    del payload["self_contact_id"]

    response = client.post("/infra/job/start", data=payload)

    assert response.status_code == 422


def test_invalid_form_returns_400(client):
    response, bootstrap = _post_start_job(client, team_ids='[1, "foo", 3]')

    assert response.status_code == 400
    assert response.json()["detail"] == "team_ids must be a list of integers"
    bootstrap.assert_not_called()


def test_invalid_team_summaries_form_returns_400(client):
    response, bootstrap = _post_start_job(
        client,
        team_summaries='[{"team_id": "not-int", "name": "Ops", "description": "Bad"}]',
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "team_summaries.team_id must be an integer"
    bootstrap.assert_not_called()
