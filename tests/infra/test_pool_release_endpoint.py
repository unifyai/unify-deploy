from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def test_release_endpoint_resolves_job_name_to_current_vm():
    session = {
        "status": {
            "jobRef": {"name": "unity-job-1"},
            "vmRef": {
                "name": "unity-pool-ubuntu-3-preview",
                "hostname": "vm-3.vm.unify.ai",
            },
        },
    }

    with (
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.release_pool_vm",
            return_value={
                "released": True,
                "assistant_id": "assistant-123",
                "vm_name": "unity-pool-ubuntu-3-preview",
                "pool_role": "releasing",
            },
        ) as mock_release_pool_vm,
    ):
        resp = _client().post(
            "/infra/vm/pool/release",
            json={
                "assistant_id": "assistant-123",
                "job_name": "unity-job-1",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["released"] is True
    mock_release_pool_vm.assert_called_once_with(
        "assistant-123",
        vm_name="unity-pool-ubuntu-3-preview",
    )


def test_release_endpoint_skips_stale_job_target():
    session = {
        "status": {
            "jobRef": {"name": "unity-job-2"},
            "vmRef": {
                "name": "unity-pool-ubuntu-3-preview",
                "hostname": "vm-3.vm.unify.ai",
            },
        },
    }

    with (
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=session,
        ),
        patch(
            "communication.infra.views.release_pool_vm",
            side_effect=AssertionError("stale job must not release current VM"),
        ),
    ):
        resp = _client().post(
            "/infra/vm/pool/release",
            json={
                "assistant_id": "assistant-123",
                "job_name": "unity-job-1",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "released": False,
        "assistant_id": "assistant-123",
        "job_name": "unity-job-1",
        "current_job_name": "unity-job-2",
        "stale": True,
        "message": "Job no longer owns the current session VM",
    }
