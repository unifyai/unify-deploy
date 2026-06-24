"""`runtime_cleanup_complete` must wait for in-flight offline Jobs to drain.

Offline task runners (``app=unity-offline``) live outside the AssistantSession
lifecycle but still emit writes against the owning body. The membership-change
runtime barrier polls ``/infra/runtime/{id}`` and gates on
``runtime_cleanup_complete``; that aggregator therefore ANDs "no Running
``unity-offline`` Jobs for this assistant" into its completion predicate so
drain callers naturally wait for offline runs to finish before proceeding.
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


def _list_jobs_factory(online_items, offline_items):
    """Return a ``list_namespaced_job`` side-effect switching on the label selector."""

    def _list_jobs(*_args, **kwargs):
        selector = kwargs["label_selector"]
        if "app=unity-offline" in selector:
            return SimpleNamespace(items=offline_items)
        if "app=unity," in selector or selector.startswith("app=unity,"):
            return SimpleNamespace(items=online_items)
        return SimpleNamespace(items=[])

    return _list_jobs


def _runtime_status_response(client, *, online_items, offline_items):
    batch_api = MagicMock()
    batch_api.list_namespaced_job.side_effect = _list_jobs_factory(
        online_items,
        offline_items,
    )

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
        return client.get("/infra/runtime/1207")


def test_runtime_cleanup_false_while_offline_job_running(client):
    """A Running ``unity-offline`` Job must block ``runtime_cleanup_complete``."""

    response = _runtime_status_response(
        client,
        online_items=[],
        offline_items=[
            _job(
                "unity-offline-run-9",
                app="unity-offline",
                assistant_id="1207",
                active=1,
            ),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active_job_names"] == []
    assert body["active_offline_job_names"] == ["unity-offline-run-9"]
    assert body["runtime_cleanup_complete"] is False


def test_runtime_cleanup_true_after_offline_job_succeeds(client):
    """A succeeded ``unity-offline`` Job (``active=None``) clears the gate."""

    response = _runtime_status_response(
        client,
        online_items=[],
        offline_items=[
            _job(
                "unity-offline-run-9",
                app="unity-offline",
                assistant_id="1207",
                active=None,
            ),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active_offline_job_names"] == []
    assert body["runtime_cleanup_complete"] is True


def test_runtime_cleanup_uses_sanitized_assistant_id_for_offline_label(client):
    """The offline label selector must reuse the same sanitization as online Jobs."""

    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[])

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
        response = client.get("/infra/runtime/Assistant_1207")

    assert response.status_code == 200
    selectors = {
        call.kwargs["label_selector"]
        for call in batch_api.list_namespaced_job.call_args_list
    }
    assert "app=unity,assistant-id=assistant-1207" in selectors
    assert "app=unity-offline,assistant-id=assistant-1207" in selectors
