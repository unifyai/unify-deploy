"""
Focused tests for POST /infra/job/start session reuse behavior.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from common.settings import SETTINGS


@pytest.fixture
def client(monkeypatch):
    from communication.infra.views import assistant_self_router, router

    # Self-scoped routes authorize with admin-or-assistant; set a known admin
    # key and send it by default so these logic-focused tests hit the admin
    # short-circuit rather than the per-assistant session lookup.
    monkeypatch.setattr(
        SETTINGS,
        "orchestra_admin_key",
        "TEST-ADMIN-KEY",
        raising=False,
    )

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    app.include_router(assistant_self_router, prefix="/infra")
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return test_client


@pytest.fixture(autouse=True)
def _mock_idle_pool_replenishment():
    with patch(
        "communication.infra.views.schedule_idle_job_pool_replenishment",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_orchestra_assistant_lookup():
    with patch(
        "communication.infra.views.get_assistant",
        return_value={
            "desktop_mode": "ubuntu",
            "managed_desktop_status": "active",
        },
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
        "user_desktops": "[]",
        "is_coordinator": "false",
        "team_ids": "[]",
        "team_ids": "[]",
        "team_summaries": "[]",
        "self_contact_id": "101",
        "boss_contact_id": "202",
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


def _control_plane_ready_patch(
    *,
    ready: bool = True,
    reason: str | None = None,
):
    return patch(
        "communication.infra.views._assistant_session_control_plane_ready",
        new_callable=AsyncMock,
        return_value=(ready, reason),
    )


def _post_start_job_and_capture_bootstrap_payload(client, form_payload: dict[str, str]):
    """Post a start-job form and return the generated bootstrap payload."""

    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session()

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
        _control_plane_ready_patch(),
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
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ),
    ):
        response = client.post("/infra/job/start", data=form_payload)

    return response, mock_create_or_update_bootstrap_secret.call_args.args[4]


class _FakeCoordApi:
    def __init__(self):
        self._leases: dict[tuple[str, str], SimpleNamespace] = {}
        self._uid_counter = 0

    def create_namespaced_lease(self, namespace, body):
        key = (namespace, body.metadata.name)
        if key in self._leases:
            raise ApiException(status=409)
        self._uid_counter += 1
        lease = SimpleNamespace(
            metadata=SimpleNamespace(
                name=body.metadata.name,
                namespace=namespace,
                uid=f"lease-{self._uid_counter}",
            ),
            spec=SimpleNamespace(
                holder_identity=body.spec.holder_identity,
                lease_duration_seconds=body.spec.lease_duration_seconds,
                acquire_time=body.spec.acquire_time,
                renew_time=body.spec.renew_time,
            ),
        )
        self._leases[key] = lease
        return lease

    def read_namespaced_lease(self, name, namespace):
        key = (namespace, name)
        lease = self._leases.get(key)
        if lease is None:
            raise ApiException(status=404)
        return lease

    def delete_namespaced_lease(self, name, namespace, body=None):
        key = (namespace, name)
        lease = self._leases.get(key)
        if lease is None:
            raise ApiException(status=404)
        expected_uid = getattr(getattr(body, "preconditions", None), "uid", None)
        if expected_uid and expected_uid != lease.metadata.uid:
            raise ApiException(status=409)
        del self._leases[key]

    def expire_lease(self, name: str, namespace: str, *, age_seconds: int) -> None:
        self._leases[(namespace, name)].spec.acquire_time = datetime.now(
            timezone.utc,
        ) - timedelta(seconds=age_seconds)


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
        _control_plane_ready_patch(),
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
    assert secret_args[3] == existing_session["spec"]["activationId"]
    assert secret_args[4]["assistant_about"] == "Newest assistant bio"
    assert secret_args[4]["voice_id"] == "voice-updated"

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
    assert "serviceUrls" not in refreshed_spec
    assert refreshed_spec["requestedAt"]


def test_start_job_personal_coordinator_sets_null_org_id_in_bootstrap_payload(client):
    """Personal Coordinators should keep org_id null in bootstrap payloads."""

    response, payload = _post_start_job_and_capture_bootstrap_payload(
        client,
        _start_job_payload(is_coordinator="true", org_id=""),
    )

    assert response.status_code == 200
    assert payload["is_coordinator"] is True
    assert payload["org_id"] is None


def test_start_job_accepts_empty_assistant_about(client):
    """Empty about must not 422 when adapters posts assistant_about=."""

    response, payload = _post_start_job_and_capture_bootstrap_payload(
        client,
        _start_job_payload(assistant_about=""),
    )

    assert response.status_code == 200
    assert payload["assistant_about"] == ""


def test_start_job_decodes_coordinator_flag_into_bootstrap_payload(client):
    """The start-job form field should become a native bool in the bootstrap payload."""

    response, payload = _post_start_job_and_capture_bootstrap_payload(
        client,
        _start_job_payload(is_coordinator="true"),
    )

    assert response.status_code == 200
    assert payload["is_coordinator"] is True


def test_start_job_defaults_missing_coordinator_flag_to_false(client):
    """Missing coordinator form fields should produce an explicit non-Coordinator value."""

    form_payload = _start_job_payload()
    form_payload.pop("is_coordinator")
    response, payload = _post_start_job_and_capture_bootstrap_payload(
        client,
        form_payload,
    )

    assert response.status_code == 200
    assert payload["is_coordinator"] is False


def test_start_job_honors_desktop_required_override(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(phase="PendingVM")

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
        _control_plane_ready_patch(),
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
        ) as mock_create_or_update_assistant_session,
        patch(
            "communication.infra.views._ensure_assistant_topic_on_wake",
            new_callable=AsyncMock,
        ),
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(desktop_required="false"),
        )

    assert response.status_code == 200
    spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert spec["desktop"] == {"required": False, "mode": "ubuntu"}


def test_start_job_persists_wake_reasons_for_pending_reused_session(client):
    """Pending reused sessions must retain wake reasons in the bootstrap secret."""

    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(phase="PendingVM")

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
        _control_plane_ready_patch(),
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
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ),
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(
                wake_reasons=json.dumps([{"type": "task_due", "task_id": 101}]),
            ),
        )

    assert response.status_code == 200
    payload = mock_create_or_update_bootstrap_secret.call_args.args[4]
    assert payload["wake_reasons"] == [{"type": "task_due", "task_id": 101}]
    assert response.json()["wake_reasons_attached_to_startup"] is True


def test_start_job_strips_wake_reasons_for_running_session(client):
    """Already-running sessions should not persist one-shot wake reasons."""

    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        phase="Active",
        binding={
            "id": "binding-desktop-123",
            "desktopUrl": "https://desktop.example.test",
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
        _control_plane_ready_patch(),
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
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ),
    ):
        response = client.post(
            "/infra/job/start",
            data=_start_job_payload(
                wake_reasons=json.dumps([{"type": "task_due", "task_id": 101}]),
            ),
        )

    assert response.status_code == 200
    payload = mock_create_or_update_bootstrap_secret.call_args.args[4]
    assert "wake_reasons" not in payload
    assert response.json()["active_session_already_running"] is True
    assert response.json()["wake_reasons_attached_to_startup"] is False
    assert response.json()["binding_id"] == "binding-desktop-123"
    assert response.json()["desktop_url"] == "https://desktop.example.test"
    assert (
        response.json()["liveview_url"]
        == "https://desktop.example.test/desktop/custom.html"
    )


def test_start_job_cleans_superseded_bootstrap_secret_after_session_repoint(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session(
        secret_name="assistant-session-bootstrap-assistant-123",
    )
    refreshed_secret_name = (
        "assistant-session-bootstrap-assistant-123-activation-existing"
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
        _control_plane_ready_patch(),
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
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_updated_session,
        ),
        patch(
            "communication.infra.views.delete_bootstrap_secret_if_owned",
            return_value=True,
        ) as mock_delete_bootstrap_secret_if_owned,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    mock_delete_bootstrap_secret_if_owned.assert_called_once_with(
        core_api,
        SETTINGS.default_namespace,
        assistant_id="assistant-123",
        activation_id=existing_session["spec"]["activationId"],
        secret_name=existing_session["spec"]["startupSecretRef"],
    )


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
        _control_plane_ready_patch(),
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
    assert refreshed_spec["desktop"] == {"mode": "ubuntu", "required": True}


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
        _control_plane_ready_patch(),
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
        _control_plane_ready_patch(),
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
        _control_plane_ready_patch(),
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


def test_start_job_adopts_winner_activation_after_first_create_conflict(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    winner_session = _existing_session(
        phase="PendingJob",
        activation_id="activation-winner",
        observed_activation_id="activation-winner",
    )
    winner_session["metadata"]["name"] = "assistant-session-assistant-123"
    create_specs: list[dict] = []
    reads = {"count": 0}

    def _get_session(*_args, **_kwargs):
        reads["count"] += 1
        if reads["count"] == 1:
            return None
        return winner_session

    def _create_or_update(_custom_api, _namespace, _assistant_id, spec):
        create_specs.append(spec)
        if len(create_specs) == 1:
            raise ApiException(status=409)
        return {
            "metadata": winner_session["metadata"],
            "spec": spec,
            "status": winner_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            side_effect=_get_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_create_or_update,
        ),
        patch(
            "communication.infra.views.uuid.uuid4",
            return_value=SimpleNamespace(hex="activation-loser"),
        ),
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    assert response.json()["activation_id"] == "activation-winner"
    assert len(create_specs) == 2
    assert create_specs[0]["activationId"] == "activation-loser"
    assert create_specs[1]["activationId"] == "activation-winner"


def test_start_job_waits_for_inflight_start_lease_and_reuses_winner_activation(client):
    core_api = MagicMock()
    coord_api = MagicMock()
    custom_api = MagicMock()
    winner_session = _existing_session(
        phase="PendingJob",
        activation_id="activation-winner",
        observed_activation_id="activation-winner",
    )
    lease_attempts = iter([False, True])

    def _create_or_update(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": winner_session["metadata"],
            "spec": spec,
            "status": winner_session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), coord_api),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.acquire_named_lease",
            side_effect=lambda *_args, **_kwargs: next(lease_attempts),
        ) as mock_acquire_named_lease,
        patch(
            "communication.infra.views.release_named_lease",
        ) as mock_release_named_lease,
        patch(
            "communication.infra.views.uuid.uuid4",
            return_value=SimpleNamespace(hex="deadbeefcafebabe"),
        ),
        patch(
            "communication.infra.views.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=winner_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=_create_or_update,
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 200
    assert response.json()["activation_id"] == "activation-winner"
    assert mock_acquire_named_lease.call_count == 2
    refreshed_spec = mock_create_or_update_assistant_session.call_args.args[3]
    assert refreshed_spec["activationId"] == "activation-winner"
    mock_release_named_lease.assert_called_once_with(
        coord_api,
        "assistant-start-assistant-123",
        SETTINGS.default_namespace,
        "job-start-assistant-123-deadbeef",
    )


def test_start_job_stale_request_cannot_release_newer_start_lease():
    from communication.infra import views

    coord_api = _FakeCoordApi()
    assistant_id = "assistant-123"
    namespace = SETTINGS.default_namespace

    with patch(
        "communication.infra.views.uuid.uuid4",
        side_effect=[
            SimpleNamespace(hex="deadbeefcafebabe"),
            SimpleNamespace(hex="cafebabedeadbeef"),
        ],
    ):
        lease_name, holder_a, _ = asyncio.run(
            views._acquire_start_job_lease(coord_api, assistant_id, namespace),
        )
        coord_api.expire_lease(
            lease_name,
            namespace,
            age_seconds=views.START_JOB_LEASE_DURATION_SECONDS + 1,
        )
        reacquired_lease_name, holder_b, _ = asyncio.run(
            views._acquire_start_job_lease(coord_api, assistant_id, namespace),
        )

    assert reacquired_lease_name == lease_name
    assert holder_a == "job-start-assistant-123-deadbeef"
    assert holder_b == "job-start-assistant-123-cafebabe"

    views.release_named_lease(coord_api, lease_name, namespace, holder_a)

    current_lease = coord_api.read_namespaced_lease(lease_name, namespace)
    assert current_lease.spec.holder_identity == holder_b

    views.release_named_lease(coord_api, reacquired_lease_name, namespace, holder_b)
    with pytest.raises(ApiException) as exc_info:
        coord_api.read_namespaced_lease(reacquired_lease_name, namespace)
    assert exc_info.value.status == 404


def test_start_job_lease_recovers_when_conflicted_lease_disappears_before_read():
    from communication.infra.helpers import acquire_named_lease

    coord_api = _FakeCoordApi()
    lease_name = "assistant-start-assistant-123"
    namespace = SETTINGS.default_namespace

    assert acquire_named_lease(
        coord_api,
        lease_name,
        namespace,
        holder_id="job-start-assistant-123-deadbeef",
        duration=30,
    )

    original_read_namespaced_lease = coord_api.read_namespaced_lease

    def _disappearing_read(name, namespace):
        coord_api.delete_namespaced_lease(name=name, namespace=namespace)
        raise ApiException(status=404)

    coord_api.read_namespaced_lease = MagicMock(side_effect=_disappearing_read)

    assert acquire_named_lease(
        coord_api,
        lease_name,
        namespace,
        holder_id="job-start-assistant-123-cafebabe",
        duration=30,
    )

    current_lease = original_read_namespaced_lease(lease_name, namespace)
    assert current_lease.spec.holder_identity == "job-start-assistant-123-cafebabe"


def test_start_job_returns_503_when_new_activation_needs_control_plane(client):
    core_api = MagicMock()

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(
            ready=False,
            reason="assistant_session_controller_missing",
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=None,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 503
    assert (
        "control plane is unavailable for new activations" in response.json()["detail"]
    )
    mock_create_or_update_bootstrap_secret.assert_not_called()
    mock_create_or_update_assistant_session.assert_not_called()


def test_start_job_rejects_reuse_of_terminating_session(client):
    core_api = MagicMock()
    custom_api = MagicMock()
    terminating_session = _existing_session()
    terminating_session["metadata"]["deletionTimestamp"] = "2026-04-06T12:00:00Z"

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.START_JOB_TERMINATING_SESSION_WAIT_TIMEOUT_SECONDS",
            0.0,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=terminating_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
        ) as mock_create_or_update_bootstrap_secret,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
        ) as mock_create_or_update_assistant_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 409
    assert "deletion is still in progress" in response.json()["detail"]
    mock_create_or_update_bootstrap_secret.assert_not_called()
    mock_create_or_update_assistant_session.assert_not_called()


def test_start_job_returns_conflict_when_session_terminates_mid_write(client):
    from communication.infra import views

    core_api = MagicMock()
    custom_api = MagicMock()
    existing_session = _existing_session()

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), core_api, MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
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
            side_effect=views.AssistantSessionTerminatingError(
                "AssistantSession assistant-session-assistant-123 is deleting",
            ),
        ),
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 409
    assert "is deleting" in response.json()["detail"]


def test_request_desktop_promotes_voice_only_session(client):
    custom_api = MagicMock()
    session = _existing_session(phase="Active", desktop_required=False)

    with (
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.patch_assistant_session_spec",
            return_value=session,
        ) as mock_patch_spec,
        patch(
            "communication.infra.views.emit_observability_event",
        ),
    ):
        response = client.post("/infra/runtime/assistant-123/request-desktop")

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": "promoted"}
    mock_patch_spec.assert_called_once()
    assert mock_patch_spec.call_args.kwargs["desktop_required"] is True


def test_request_desktop_is_idempotent_when_already_required(client):
    custom_api = MagicMock()
    session = _existing_session(phase="Active", desktop_required=True)

    with (
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.patch_assistant_session_spec",
        ) as mock_patch_spec,
    ):
        response = client.post("/infra/runtime/assistant-123/request-desktop")

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": "already_required"}
    mock_patch_spec.assert_not_called()


def _bound_desktop_binding() -> dict:
    return {
        "id": "binding-with-vm",
        "vmRef": {
            "name": "unity-pool-ubuntu-1-staging",
            "hostname": "unity-pool-ubuntu-1-staging.vm.unify.ai",
            "vmType": "ubuntu",
        },
        "desktopUrl": "https://unity-pool-ubuntu-1-staging.vm.unify.ai",
    }


def _start_job_spec_for(client, session, **payload_overrides):
    """Post /infra/job/start against ``session`` and return the written spec."""

    def _updated_session(_custom_api, _namespace, _assistant_id, spec):
        return {
            "metadata": session["metadata"],
            "spec": spec,
            "status": session["status"],
        }

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
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
            data=_start_job_payload(**payload_overrides),
        )

    assert response.status_code == 200
    return mock_create_or_update_assistant_session.call_args.args[3]


def test_start_job_refuses_desktop_downgrade_for_bound_binding(client):
    """A voice wake must not demote a binding that already owns a pool VM.

    Honouring ``desktop_required=false`` here made the controller clear the
    vmRef while the VM and the assistant disk stayed labelled for the binding,
    so every later assignment deadlocked on its own leftover disk.
    """
    session = _existing_session(
        phase="PendingGuest",
        binding=_bound_desktop_binding(),
    )

    spec = _start_job_spec_for(
        client,
        session,
        medium="unify_meet",
        desktop_required="false",
    )

    assert spec["desktop"]["required"] is True
    assert spec["desktop"]["mode"] == "ubuntu"


def test_start_job_refuses_desktop_downgrade_while_assignment_in_progress(client):
    """An in-flight assignment counts as bound: the worker may already hold a VM."""
    session = _existing_session(
        phase="PendingVM",
        binding={
            "id": "binding-assigning",
            "vmAssignment": {"state": "in_progress", "attemptId": "attempt-1"},
        },
    )

    spec = _start_job_spec_for(
        client,
        session,
        medium="unify_meet",
        desktop_required="false",
    )

    assert spec["desktop"]["required"] is True


def test_start_job_still_defers_desktop_before_any_vm_is_bound(client):
    """The voice latency optimisation survives for sessions with no VM yet."""
    session = _existing_session(phase="PendingContainer", binding={})

    spec = _start_job_spec_for(
        client,
        session,
        medium="unify_meet",
        desktop_required="false",
    )

    assert spec["desktop"]["required"] is False
    assert "placement" not in spec["desktop"]


def test_start_job_still_defers_desktop_while_release_is_draining(client):
    """A draining binding is being torn down, so a fresh wake may defer again."""
    session = _existing_session(
        phase="Released",
        binding=_bound_desktop_binding(),
    )

    spec = _start_job_spec_for(
        client,
        session,
        medium="unify_meet",
        desktop_required="false",
    )

    assert spec["desktop"]["required"] is False
