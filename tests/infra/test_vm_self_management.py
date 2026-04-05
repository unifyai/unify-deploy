from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_vm(status: str = "RUNNING", pool_role: str = "starting"):
    vm = MagicMock()
    vm.status = status
    vm.labels = {"pool-role": pool_role}
    return vm


@pytest.fixture
def client():
    from communication.dependencies import authenticate_vm_identity
    from communication.infra.views import vm_self_router

    app = FastAPI()
    app.include_router(vm_self_router, prefix="/infra")
    app.dependency_overrides[authenticate_vm_identity] = lambda: {
        "google": {
            "compute_engine": {
                "instance_name": "unity-pool-ubuntu-1-preview",
            },
        },
    }
    return TestClient(app)


def test_vm_mark_idle_uses_role_cas_and_skips_if_role_changed(client):
    initial_vm = _make_vm(pool_role="starting")
    refreshed_vm = _make_vm(pool_role="quarantined")

    with (
        patch(
            "communication.infra.views.compute_v1.InstancesClient",
        ) as mock_client_cls,
        patch(
            "communication.infra.views._set_pool_labels",
            return_value=False,
        ) as mock_set_pool_labels,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get.side_effect = [initial_vm, refreshed_vm]

        resp = client.post("/infra/vm/mark-idle")

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-preview",
        "pool_role": "quarantined",
        "skipped": True,
        "reason": "role_changed",
    }
    mock_set_pool_labels.assert_called_once_with(
        mock_client,
        "unity-pool-ubuntu-1-preview",
        {"pool-role": "idle"},
        expected_role="starting",
    )


def test_vm_release_complete_triggers_trim_after_idle_transition(client):
    vm = _make_vm(pool_role="releasing")
    vm.labels.update(
        {
            "assistant-id": "1207",
            "binding-id": "binding-123",
        },
    )
    session = {
        "status": {
            "binding": {
                "id": "binding-123",
                "jobRef": {"name": "unity-job-1"},
                "vmRef": {"name": "unity-pool-ubuntu-1-preview"},
                "releaseRequestedAt": "2026-04-05T15:39:57Z",
            },
        },
    }

    with (
        patch(
            "communication.infra.views.compute_v1.InstancesClient",
        ) as mock_client_cls,
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.patch_assistant_session_status",
        ) as patch_status,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-preview",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "accepted": True,
    }
    updated_binding = patch_status.call_args.kwargs["binding"]
    assert updated_binding["id"] == "binding-123"
    assert updated_binding["jobRef"]["name"] == "unity-job-1"
    assert updated_binding["vmRef"]["name"] == "unity-pool-ubuntu-1-preview"
    assert updated_binding["releaseRequestedAt"] == "2026-04-05T15:39:57Z"
    assert updated_binding["releaseCompletedAt"]
    assert patch_status.call_args.kwargs["source"] == "views.release_complete"


def test_vm_release_complete_triggers_replenish_after_retirement(client):
    vm = _make_vm(pool_role="releasing")
    vm.labels.update(
        {
            "assistant-id": "1207",
            "binding-id": "binding-123",
        },
    )

    with (
        patch(
            "communication.infra.views.compute_v1.InstancesClient",
        ) as mock_client_cls,
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=None,
        ),
        patch(
            "communication.infra.views.patch_assistant_session_status",
        ) as patch_status,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-preview",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "skipped": True,
        "reason": "session_missing",
    }
    patch_status.assert_not_called()
