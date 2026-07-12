"""`runtime_cleanup_complete` gates on AssistantSession Jobs, not a parallel lane.

Offline work shares ``app=unity`` AssistantSession pods with live traffic, so
quiescence is ``session Released`` + no active ``app=unity`` Jobs (+ VMs/disk).
``active_offline_job_names`` remains in the API response as an empty list for
older drain callers.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _job(name: str, *, app: str, assistant_id: str, active: int):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"app": app, "assistant-id": assistant_id},
            annotations={},
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(active=active),
    )


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def _runtime_status_response(client, *, online_items):
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=online_items)

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=None,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=None,
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
        return client.get("/infra/runtime/assistant-1207"), batch_api


def test_runtime_cleanup_blocks_on_active_unity_job(client):
    """A Running ``app=unity`` Job must block ``runtime_cleanup_complete``."""

    response, batch_api = _runtime_status_response(
        client,
        online_items=[
            _job(
                "unity-assistant-1207",
                app="unity",
                assistant_id="assistant-1207",
                active=1,
            ),
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["active_job_names"] == ["unity-assistant-1207"]
    assert body["active_offline_job_names"] == []
    assert body["runtime_cleanup_complete"] is False
    selectors = [
        call.kwargs.get("label_selector", "")
        for call in batch_api.list_namespaced_job.call_args_list
    ]
    assert any("app=unity,assistant-id=assistant-1207" in s for s in selectors)
    assert not any("app=unity-offline" in s for s in selectors)


def test_runtime_cleanup_complete_when_no_active_jobs(client):
    """No active Jobs and no session → cleanup complete."""

    response, _batch_api = _runtime_status_response(client, online_items=[])
    assert response.status_code == 200
    body = response.json()
    assert body["active_job_names"] == []
    assert body["active_offline_job_names"] == []
    assert body["runtime_cleanup_complete"] is True
