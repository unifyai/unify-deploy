from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


# /vm/pool/release is a self-scoped route; the admin key makes the admin
# short-circuit apply, since these tests target release logic rather than auth.
@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "TEST-ADMIN-KEY")


def _views_module():
    import communication.infra.views as views_module

    return views_module


def _client() -> TestClient:
    from communication.infra.views import assistant_self_router, router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    app.include_router(assistant_self_router, prefix="/infra")
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return test_client


def test_release_endpoint_resolves_job_name_to_current_vm():
    views_module = _views_module()
    session = {
        "status": {
            "binding": {
                "id": "binding-1",
                "jobRef": {"name": "unity-job-1"},
                "vmRef": {
                    "name": "unity-pool-ubuntu-3-staging",
                    "hostname": "vm-3.vm.unify.ai",
                },
            },
        },
    }

    with (
        patch.object(views_module, "get_custom_objects_api", return_value=MagicMock()),
        patch.object(views_module, "get_assistant_session", return_value=session),
        patch.object(
            views_module,
            "release_pool_vm",
            return_value={
                "released": True,
                "assistant_id": "assistant-123",
                "vm_name": "unity-pool-ubuntu-3-staging",
                "pool_role": "releasing",
            },
        ) as mock_release_pool_vm,
    ):
        resp = _client().post(
            "/infra/vm/pool/release",
            json={
                "assistant_id": "assistant-123",
                "binding_id": "binding-1",
                "job_name": "unity-job-1",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["released"] is True
    mock_release_pool_vm.assert_called_once_with(
        "assistant-123",
        "binding-1",
        vm_name="unity-pool-ubuntu-3-staging",
        release_generation=None,
    )


def test_release_endpoint_recovers_missing_binding_vm_ref_from_runtime_owner():
    views_module = _views_module()
    session = {
        "status": {
            "binding": {
                "id": "binding-1",
                "jobRef": {"name": "unity-job-1"},
            },
        },
    }

    with (
        patch.object(views_module, "get_custom_objects_api", return_value=MagicMock()),
        patch.object(views_module, "get_assistant_session", return_value=session),
        patch.object(
            views_module,
            "split_binding_runtime_vms",
            return_value=(
                [
                    {
                        "binding_id": "binding-1",
                        "pool_role": "assigned",
                        "vm_name": "unity-pool-ubuntu-3-staging",
                        "hostname": "vm-3.vm.unify.ai",
                        "vm_type": "ubuntu",
                    },
                ],
                [],
            ),
        ),
        patch.object(
            views_module,
            "find_vm_with_disk",
            return_value="unity-pool-ubuntu-3-staging",
        ),
        patch.object(
            views_module,
            "release_pool_vm",
            return_value={
                "released": True,
                "assistant_id": "assistant-123",
                "vm_name": "unity-pool-ubuntu-3-staging",
                "pool_role": "releasing",
            },
        ) as mock_release_pool_vm,
    ):
        resp = _client().post(
            "/infra/vm/pool/release",
            json={
                "assistant_id": "assistant-123",
                "binding_id": "binding-1",
                "job_name": "unity-job-1",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["released"] is True
    mock_release_pool_vm.assert_called_once_with(
        "assistant-123",
        "binding-1",
        vm_name="unity-pool-ubuntu-3-staging",
        release_generation=None,
    )


def test_release_endpoint_skips_stale_job_target():
    views_module = _views_module()
    session = {
        "status": {
            "binding": {
                "id": "binding-1",
                "jobRef": {"name": "unity-job-2"},
                "vmRef": {
                    "name": "unity-pool-ubuntu-3-staging",
                    "hostname": "vm-3.vm.unify.ai",
                },
            },
        },
    }

    with (
        patch.object(views_module, "get_custom_objects_api", return_value=MagicMock()),
        patch.object(views_module, "get_assistant_session", return_value=session),
        patch.object(
            views_module,
            "release_pool_vm",
            side_effect=AssertionError("stale job must not release current VM"),
        ),
    ):
        resp = _client().post(
            "/infra/vm/pool/release",
            json={
                "assistant_id": "assistant-123",
                "binding_id": "binding-1",
                "job_name": "unity-job-1",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "released": False,
        "assistant_id": "assistant-123",
        "binding_id": "binding-1",
        "job_name": "unity-job-1",
        "current_job_name": "unity-job-2",
        "stale": True,
        "message": "Job no longer owns the current session VM",
    }
