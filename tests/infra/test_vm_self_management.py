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
