"""``POST /infra/vm/pool/assign`` refuses assistants Orchestra does not know.

The assignment names a persistent disk, a static IP and a DNS record after
the assistant id, none of which the session teardown path removes. An id
that does not exist in this deployment's Orchestra must be turned away
before any of them is created.
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "TEST-ADMIN-KEY")


def _client() -> TestClient:
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return test_client


def _assign_request(assistant_id: str) -> dict[str, str]:
    return {
        "assistant_id": assistant_id,
        "binding_id": "binding-1",
        "unify_apikey": "test-unify-key",
        "vm_type": "ubuntu",
    }


def test_assign_endpoint_returns_404_for_assistant_unknown_to_orchestra():
    with (
        patch(
            "communication.infra.views.get_assistant",
            return_value={"assistant_id": None, "desktop_mode": "none"},
        ),
        patch("communication.infra.views.assign_pool_vm") as mock_assign_pool_vm,
        patch("communication.infra.views.replenish_pool") as mock_replenish_pool,
    ):
        response = _client().post(
            "/infra/vm/pool/assign",
            json=_assign_request("8140"),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Assistant 8140 does not exist in Orchestra"
    mock_assign_pool_vm.assert_not_called()
    mock_replenish_pool.assert_not_called()


def test_assign_endpoint_assigns_for_known_assistant():
    assignment = {
        "vm_name": "unity-pool-ubuntu-3-staging",
        "assistant_id": "assistant-123",
        "ip_address": "10.0.0.3",
        "hostname": "unity-pool-ubuntu-3-staging.vm.unify.ai",
        "desktop_url": "https://unity-pool-ubuntu-3-staging.vm.unify.ai",
        "status": "assigned",
        "ssh_username": "unityuser",
        "ssh_port": 2222,
    }

    with (
        patch(
            "communication.infra.views.get_assistant",
            return_value={
                "assistant_id": "assistant-123",
                "desktop_mode": "ubuntu",
                "managed_desktop_status": "active",
            },
        ),
        patch(
            "communication.infra.views.assign_pool_vm",
            return_value=assignment,
        ) as mock_assign_pool_vm,
        patch("communication.infra.views.replenish_pool"),
    ):
        response = _client().post(
            "/infra/vm/pool/assign",
            json=_assign_request("assistant-123"),
        )

    assert response.status_code == 200
    assert response.json()["vm_name"] == assignment["vm_name"]
    mock_assign_pool_vm.assert_called_once()
    assert mock_assign_pool_vm.call_args.kwargs["assistant_id"] == "assistant-123"
