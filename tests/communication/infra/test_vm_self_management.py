import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.settings import SETTINGS


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
                "instance_name": "unity-pool-ubuntu-1-staging",
                "project_id": SETTINGS.vm_project_id,
                "zone": f"projects/{SETTINGS.vm_project_id}/zones/{SETTINGS.vm_zone}",
            },
        },
    }
    return TestClient(app)


@pytest.fixture
def regional_client():
    from communication.dependencies import authenticate_vm_identity
    from communication.infra.views import vm_self_router

    app = FastAPI()
    app.include_router(vm_self_router, prefix="/infra")
    app.dependency_overrides[authenticate_vm_identity] = lambda: {
        "google": {
            "compute_engine": {
                "instance_name": "unity-pool-ubuntu-europe-west2-1-staging",
                "project_id": SETTINGS.vm_project_id,
                "zone": (f"projects/{SETTINGS.vm_project_id}/zones/europe-west2-a"),
            },
        },
    }
    return TestClient(app)


@pytest.fixture
def tunnel_client():
    from communication.infra.views import tunnel_router

    app = FastAPI()
    app.include_router(tunnel_router, prefix="/infra")
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
        "vm_name": "unity-pool-ubuntu-1-staging",
        "pool_role": "quarantined",
        "skipped": True,
        "reason": "role_changed",
    }
    mock_set_pool_labels.assert_called_once_with(
        mock_client,
        "unity-pool-ubuntu-1-staging",
        {"pool-role": "idle"},
        expected_role="starting",
    )


def test_vm_mark_idle_uses_calling_regional_zone(regional_client):
    seen_zones = []

    def set_labels(*_args, **_kwargs):
        from communication.infra.vm_helpers import _current_vm_placement

        seen_zones.append(_current_vm_placement().zone)
        return True

    with (
        patch(
            "communication.infra.views.compute_v1.InstancesClient",
        ) as mock_client_cls,
        patch(
            "communication.infra.views._set_pool_labels",
            side_effect=set_labels,
        ),
    ):
        mock_client_cls.return_value.get.return_value = _make_vm()
        response = regional_client.post("/infra/vm/mark-idle")

    assert response.status_code == 200
    assert mock_client_cls.return_value.get.call_args.kwargs["zone"] == "europe-west2-a"
    assert seen_zones == ["europe-west2-a"]


def test_vm_wipe_metadata_key_uses_calling_regional_zone(regional_client):
    seen_zones = []

    def update_metadata(*_args, **_kwargs):
        from communication.infra.vm_helpers import _current_vm_placement

        seen_zones.append(_current_vm_placement().zone)

    with patch(
        "communication.infra.views._update_instance_metadata",
        side_effect=update_metadata,
    ) as update_metadata_mock:
        response = regional_client.post(
            "/infra/vm/wipe-metadata-key",
            json={"key": "github-token"},
        )

    assert response.status_code == 200
    update_metadata_mock.assert_called_once_with(
        "unity-pool-ubuntu-europe-west2-1-staging",
        {"github-token": ""},
    )
    assert seen_zones == ["europe-west2-a"]


def test_vm_release_complete_records_signal_for_active_binding(client):
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
                "vmRef": {"name": "unity-pool-ubuntu-1-staging"},
                "releaseRequestedAt": "2026-04-05T15:39:57Z",
                "releaseGeneration": 2,
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
            "communication.infra.views.complete_pool_vm_release",
            return_value={
                "vm_name": "unity-pool-ubuntu-1-staging",
                "binding_id": "binding-123",
                "pool_role": "idle",
            },
        ) as complete_release,
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123", "release_generation": 2},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-staging",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "release_generation": 2,
        "accepted": True,
    }
    complete_release.assert_called_once_with(
        "unity-pool-ubuntu-1-staging",
        "binding-123",
    )
    assert record_signal.call_args.kwargs["signal_name"] == "vmReleaseComplete"
    assert record_signal.call_args.kwargs["payload"]["bindingId"] == "binding-123"
    assert (
        record_signal.call_args.kwargs["payload"]["vmName"]
        == "unity-pool-ubuntu-1-staging"
    )
    assert record_signal.call_args.kwargs["payload"]["releaseGeneration"] == 2
    assert record_signal.call_args.kwargs["source"] == "views.release_complete"


def test_vm_release_complete_skips_signal_when_session_is_missing(client):
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
            "communication.infra.views.complete_pool_vm_release",
            return_value={
                "vm_name": "unity-pool-ubuntu-1-staging",
                "binding_id": "binding-123",
                "pool_role": "idle",
            },
        ) as complete_release,
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=None,
        ),
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-staging",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "release_generation": None,
        "accepted": True,
        "reason": "session_missing",
    }
    complete_release.assert_called_once_with(
        "unity-pool-ubuntu-1-staging",
        "binding-123",
    )
    record_signal.assert_not_called()


def test_vm_release_complete_accepts_when_session_api_is_unavailable(client):
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
            "communication.infra.views.complete_pool_vm_release",
            return_value={
                "vm_name": "unity-pool-ubuntu-1-staging",
                "binding_id": "binding-123",
                "pool_role": "idle",
            },
        ) as complete_release,
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=None,
        ),
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-staging",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "release_generation": None,
        "accepted": True,
        "reason": "session_api_unavailable",
    }
    complete_release.assert_called_once_with(
        "unity-pool-ubuntu-1-staging",
        "binding-123",
    )
    record_signal.assert_not_called()


def test_vm_release_complete_skips_stale_release_generation(client):
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
                "vmRef": {"name": "unity-pool-ubuntu-1-staging"},
                "releaseRequestedAt": "2026-04-05T15:39:57Z",
                "releaseGeneration": 2,
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
            "communication.infra.views.complete_pool_vm_release",
            return_value={
                "vm_name": "unity-pool-ubuntu-1-staging",
                "binding_id": "binding-123",
                "pool_role": "idle",
            },
        ) as complete_release,
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        mock_client_cls.return_value.get.return_value = vm
        resp = client.post(
            "/infra/vm/release-complete",
            json={"binding_id": "binding-123", "release_generation": 1},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "vm_name": "unity-pool-ubuntu-1-staging",
        "assistant_id": "1207",
        "binding_id": "binding-123",
        "release_generation": 1,
        "current_release_generation": 2,
        "accepted": True,
        "reason": "release_generation_changed",
    }
    complete_release.assert_called_once_with(
        "unity-pool-ubuntu-1-staging",
        "binding-123",
    )
    record_signal.assert_not_called()


def test_vm_ready_records_desktop_ready_signal_for_active_binding(tunnel_client):
    session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-1",
            "conditions": [{"type": "ContainerReady", "status": "True"}],
            "binding": {
                "id": "binding-123",
                "vmRef": {
                    "name": "unity-pool-ubuntu-1-staging",
                    "hostname": "vm-1.vm.unify.ai",
                },
                "desktopSecret": "vm-secret-abc",
            },
        },
    }

    with (
        patch(
            "communication.infra.views.extract_api_key",
            return_value="user-key",
        ),
        patch(
            "communication.infra.views.authenticate_user_api_key",
            new=AsyncMock(),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views._get_k8s_clients",
            new=AsyncMock(return_value=(None, object(), None, None)),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.read_bootstrap_secret",
            return_value={"api_key": "user-key"},
        ),
        patch(
            "communication.infra.views.verify_vm_assignment",
            return_value={
                "name": "unity-pool-ubuntu-1-staging",
                "hostname": "vm-1.vm.unify.ai",
            },
        ),
        patch(
            "communication.infra.views.probe_vm_agent_service_authenticated",
            return_value=True,
        ),
        patch(
            "communication.infra.views._publish_desktop_ready",
            new=AsyncMock(return_value="message-123"),
        ) as publish_desktop_ready,
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        resp = tunnel_client.post(
            "/infra/vm/ready",
            json={
                "assistant_id": "1207",
                "binding_id": "binding-123",
                "hostname": "vm-1.vm.unify.ai",
                "vm_type": "ubuntu",
            },
            headers={"Authorization": "Bearer user-key"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "message_id": "message-123",
        "assistant_id": "1207",
        "mode": "session",
    }
    publish_desktop_ready.assert_awaited_once_with(
        "1207",
        "vm-1.vm.unify.ai",
        "ubuntu",
        binding_id="binding-123",
        desktop_secret="vm-secret-abc",
    )
    assert record_signal.call_args.kwargs["signal_name"] == "desktopReady"
    assert record_signal.call_args.kwargs["payload"]["bindingId"] == "binding-123"
    assert (
        record_signal.call_args.kwargs["payload"]["desktopUrl"]
        == "https://vm-1.vm.unify.ai"
    )
    assert record_signal.call_args.kwargs["payload"]["messageId"] == "message-123"


@pytest.mark.asyncio
async def test_publish_desktop_ready_includes_secret_when_set():
    from communication.infra import views as views_module

    publisher = MagicMock()
    publisher.topic_path.return_value = "projects/test/topics/unity-1207-staging"
    future = MagicMock()
    future.result.return_value = "message-999"
    publisher.publish.return_value = future

    with patch.object(
        views_module,
        "_get_pubsub_clients",
        return_value=(publisher, MagicMock()),
    ):
        message_id = await views_module._publish_desktop_ready(
            "1207",
            "vm-1.vm.unify.ai",
            "ubuntu",
            binding_id="binding-123",
            desktop_secret="vm-secret-abc",
        )

    assert message_id == "message-999"
    payload = json.loads(publisher.publish.call_args.kwargs["data"])
    assert payload["event"]["desktop_secret"] == "vm-secret-abc"


@pytest.mark.asyncio
async def test_publish_desktop_ready_omits_secret_when_absent():
    from communication.infra import views as views_module

    publisher = MagicMock()
    publisher.topic_path.return_value = "projects/test/topics/unity-1207-staging"
    future = MagicMock()
    future.result.return_value = "message-998"
    publisher.publish.return_value = future

    with patch.object(
        views_module,
        "_get_pubsub_clients",
        return_value=(publisher, MagicMock()),
    ):
        await views_module._publish_desktop_ready(
            "1207",
            "vm-1.vm.unify.ai",
            "ubuntu",
            binding_id="binding-123",
        )

    payload = json.loads(publisher.publish.call_args.kwargs["data"])
    assert "desktop_secret" not in payload["event"]


def test_vm_ready_ignores_release_in_progress_for_current_binding(tunnel_client):
    session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desiredState": "Stopped",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-1",
            "conditions": [],
            "binding": {
                "id": "binding-123",
                "vmRef": {
                    "name": "unity-pool-ubuntu-1-staging",
                    "hostname": "vm-1.vm.unify.ai",
                },
                "releaseRequestedAt": "2026-04-06T18:40:00Z",
            },
        },
    }

    with (
        patch(
            "communication.infra.views.extract_api_key",
            return_value="user-key",
        ),
        patch(
            "communication.infra.views.authenticate_user_api_key",
            new=AsyncMock(),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=object(),
        ),
        patch(
            "communication.infra.views._get_k8s_clients",
            new=AsyncMock(return_value=(None, object(), None, None)),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.read_bootstrap_secret",
            return_value={"api_key": "user-key"},
        ),
        patch(
            "communication.infra.views.verify_vm_assignment",
            side_effect=AssertionError(
                "release in progress should skip vm ownership checks",
            ),
        ),
        patch(
            "communication.infra.views.probe_vm_agent_service_authenticated",
            side_effect=AssertionError("release in progress should skip guest probing"),
        ),
        patch(
            "communication.infra.views._publish_desktop_ready",
            new=AsyncMock(),
        ) as publish_desktop_ready,
        patch(
            "communication.infra.views.record_assistant_session_signal",
        ) as record_signal,
    ):
        resp = tunnel_client.post(
            "/infra/vm/ready",
            json={
                "assistant_id": "1207",
                "binding_id": "binding-123",
                "hostname": "vm-1.vm.unify.ai",
                "vm_type": "ubuntu",
            },
            headers={"Authorization": "Bearer user-key"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "accepted": False,
        "reason": "release_in_progress",
    }
    publish_desktop_ready.assert_not_awaited()
    record_signal.assert_not_called()
