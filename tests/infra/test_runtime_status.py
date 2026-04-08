from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from communication.infra.assistant_sessions import build_binding


def _job(name: str, *, assistant_id: str, binding_id: str):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={
                "app": "unity",
                "assistant-id": assistant_id,
                "assistantsession.unify.ai/binding-id": binding_id,
            },
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(active=1),
    )


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def test_stop_session_returns_current_binding_id(client):
    session = {
        "spec": {"assistantId": "1207", "desiredState": "Running"},
        "status": {"binding": build_binding(binding_id="binding-123")},
    }
    updated = {
        "spec": {"assistantId": "1207", "desiredState": "Stopped"},
        "status": session["status"],
    }

    with (
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.patch_assistant_session_spec",
            return_value=updated,
        ),
    ):
        response = client.post("/infra/session/1207/stop")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "assistant_id": "1207",
        "stopped": True,
        "desired_state": "Stopped",
        "binding_id": "binding-123",
    }


def test_runtime_status_reports_binding_cleanup_after_release_while_new_binding_runs(
    client,
):
    batch_api = MagicMock()
    assistant_session = {
        "spec": {
            "assistantId": "1207",
            "desiredState": "Running",
        },
        "status": {
            "phase": "PendingJob",
            "binding": build_binding(binding_id="binding-new"),
            "releasedBindings": [
                {
                    "bindingId": "binding-old",
                    "releaseRequestedAt": "2026-04-08T00:00:00+00:00",
                    "releaseCompletedAt": "2026-04-08T00:01:00+00:00",
                },
            ],
        },
    }
    current_vm = {
        "assistant_id": "1207",
        "binding_id": "binding-new",
        "pool_role": "assigned",
        "vm_name": "unity-pool-ubuntu-9-preview",
    }

    def _list_jobs(*_args, **kwargs):
        selector = kwargs["label_selector"]
        if "assistantsession.unify.ai/binding-id=binding-old" in selector:
            return SimpleNamespace(items=[])
        return SimpleNamespace(
            items=[
                _job("unity-job-new", assistant_id="1207", binding_id="binding-new"),
            ],
        )

    def _split_binding_runtime_vms(_assistant_id, *, binding_id=None):
        if binding_id == "binding-old":
            return [], [current_vm]
        if binding_id == "binding-new":
            return [current_vm], []
        return [current_vm], []

    batch_api.list_namespaced_job.side_effect = _list_jobs

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=assistant_session,
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            side_effect=_split_binding_runtime_vms,
        ),
        patch(
            "communication.infra.views.find_vm_with_disk",
            return_value="unity-pool-ubuntu-9-preview",
        ),
    ):
        response = client.get(
            "/infra/runtime/1207",
            params={"binding_id": "binding-old"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_cleanup_complete"] is False
    assert body["binding_runtime_cleanup_complete"] is True
    assert body["binding_release_recorded"] is True
    assert body["binding_release_completed_at"] == "2026-04-08T00:01:00+00:00"
    assert body["binding_active_job_names"] == []
    assert body["binding_owned_vms"] == []


def test_runtime_status_binding_cleanup_waits_for_release_record(client):
    batch_api = MagicMock()
    assistant_session = {
        "spec": {
            "assistantId": "1207",
            "desiredState": "Running",
        },
        "status": {
            "phase": "PendingJob",
            "binding": build_binding(binding_id="binding-new"),
            "releasedBindings": [],
        },
    }

    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[])

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=assistant_session,
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            return_value=([], []),
        ),
        patch(
            "communication.infra.views.find_vm_with_disk",
            return_value=None,
        ),
    ):
        response = client.get(
            "/infra/runtime/1207",
            params={"binding_id": "binding-old"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["binding_release_recorded"] is False
    assert body["binding_runtime_cleanup_complete"] is False
