"""Production-path tests for Communication provider-event offline dispatch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from communication.infra import task_activation


def _dispatch_payload(**overrides) -> dict:
    payload = {
        "contract_version": "1",
        "operation_id": "op-flow-1",
        "run_id": 4242,
        "run_key": "offline:provider_event:assistant-123:101:binding-1:rev123:abcdef0123456789",
        "assistant_id": "assistant-123",
        "task_id": 101,
        "binding_id": "binding-1",
        "receipt_id": "receipt-1",
        "accepted_activation_revision": "rev-123",
        "source_type": "provider_event",
        "dispatch_mode": "offline",
        "event_context_ref": "blob://binding-1/receipt-1",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "audience": "communication:provider-event-dispatch",
    }
    payload.update(overrides)
    return payload


def _activation() -> dict:
    return {
        "activation_kind": "provider_event",
        "execution_mode": "offline",
        "activation_revision": "rev-123",
        "source_task_log_id": 555,
        "entrypoint": 777,
        "task_name": "Issue triage",
        "task_description": "Triage new GitHub issues.",
    }


def _assistant_data() -> dict:
    return {
        "assistant_id": "assistant-123",
        "team_ids": [],
        "self_contact_id": 42,
        "boss_contact_id": 43,
        "api_key": "assistant-key",
    }


@pytest.fixture
def provider_event_client(tmp_path, monkeypatch):
    from common.settings import SETTINGS
    from communication.dependencies import auth_admin_key
    from fastapi import Depends

    SETTINGS.orchestra_admin_key = "TEST-ADMIN-KEY"
    SETTINGS.provider_event_dispatch_inbox_path = str(
        tmp_path / "provider-event-dispatch.sqlite3",
    )
    task_activation._provider_event_dispatch_inbox = None

    app = FastAPI()
    app.include_router(
        task_activation.router,
        prefix="/infra",
        dependencies=[Depends(auth_admin_key)],
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return client


@pytest.fixture
def orchestra_and_k8s_boundary(monkeypatch):
    launch_calls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        if url.endswith("/admin/task-run/get"):
            if json["run_key"] != _dispatch_payload()["run_key"]:
                response.json.return_value = {"run": None}
                return response
            response.json.return_value = {
                "run": {
                    "run_id": 4242,
                    "run_key": json["run_key"],
                    "source_type": "provider_event",
                    "execution_mode": "offline",
                    "state": "pending",
                },
            }
            return response
        if url.endswith("/admin/task-run/update"):
            response.json.return_value = {
                "run": {
                    "run_id": 4242,
                    "run_key": json["run_key"],
                    "state": "running",
                    "job_name": json["updates"]["job_name"],
                },
            }
            return response
        if url.endswith("/admin/task-activation/current"):
            response.json.return_value = {"activation": _activation()}
            return response
        raise AssertionError(f"unexpected Orchestra POST {url} {json}")

    batch_api = MagicMock()
    core_api = MagicMock()

    def fake_create_job(**kwargs):
        launch_calls.append(kwargs["body"]["metadata"]["name"])
        job = MagicMock()
        job.metadata.uid = "job-uid-1"
        return job

    batch_api.create_namespaced_job.side_effect = fake_create_job
    core_api.create_namespaced_secret.return_value = MagicMock()
    core_api.patch_namespaced_secret.return_value = MagicMock()

    async def fake_get_k8s_clients():
        return batch_api, core_api, MagicMock(), MagicMock()

    monkeypatch.setattr(task_activation.requests, "post", fake_post)
    monkeypatch.setattr(task_activation, "_get_k8s_clients", fake_get_k8s_clients)
    monkeypatch.setattr(
        task_activation,
        "_get_assistant_data",
        lambda assistant_id: _assistant_data(),
    )

    return {"launch_calls": launch_calls, "batch_api": batch_api}


def test_provider_event_dispatch_route_launches_one_job_and_records_status(
    provider_event_client,
    orchestra_and_k8s_boundary,
):
    first = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(),
    )
    assert first.status_code == 200, first.json()
    first_body = first.json()
    assert first_body["success"] is True
    assert first_body["status"] == "started"
    assert first_body["adopted_only"] is False
    assert first_body["job_name"]

    second = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(),
    )
    assert second.status_code == 200, second.json()
    second_body = second.json()
    assert second_body["status"] == "started"
    assert second_body["adopted_only"] is True
    assert len(orchestra_and_k8s_boundary["launch_calls"]) == 1

    status = provider_event_client.get(
        "/infra/task-activation/provider-event-dispatch/op-flow-1",
    )
    assert status.status_code == 200, status.json()
    assert status.json()["status"] == "started"
    assert status.json()["job_name"] == first_body["job_name"]


def test_concurrent_provider_event_dispatch_requests_create_one_job(
    provider_event_client,
    orchestra_and_k8s_boundary,
):
    def post_once(_index: int):
        return provider_event_client.post(
            "/infra/task-activation/provider-event-dispatch",
            json=_dispatch_payload(),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(post_once, range(4)))

    assert all(response.status_code == 200 for response in responses)
    assert len(orchestra_and_k8s_boundary["launch_calls"]) == 1
    started = [
        response.json() for response in responses if not response.json()["adopted_only"]
    ]
    adopted = [
        response.json() for response in responses if response.json()["adopted_only"]
    ]
    assert len(started) == 1
    assert len(adopted) == 3


@pytest.mark.parametrize(
    ("override", "expected_status", "expected_reason"),
    [
        ({"audience": "unity:provider-event-dispatch"}, 400, "invalid_audience"),
        ({"dispatch_mode": "live"}, 400, "invalid_dispatch_mode"),
        (
            {
                "issued_at": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
            },
            400,
            "dispatch_request_expired",
        ),
        ({"run_id": 9999}, 400, "run_id_mismatch"),
    ],
)
def test_provider_event_dispatch_rejects_invalid_authorization_without_launch(
    provider_event_client,
    orchestra_and_k8s_boundary,
    override,
    expected_status,
    expected_reason,
):
    response = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(**override),
    )
    assert response.status_code == expected_status, response.json()
    assert response.json()["detail"]["reason"] == expected_reason
    assert orchestra_and_k8s_boundary["launch_calls"] == []


def test_provider_event_dispatch_requires_admin_authorization(
    tmp_path,
    orchestra_and_k8s_boundary,
):
    from common.settings import SETTINGS
    from communication.dependencies import auth_admin_key
    from fastapi import Depends

    SETTINGS.orchestra_admin_key = "TEST-ADMIN-KEY"
    SETTINGS.provider_event_dispatch_inbox_path = str(
        tmp_path / "provider-event-dispatch-auth.sqlite3",
    )
    task_activation._provider_event_dispatch_inbox = None

    app = FastAPI()
    app.include_router(
        task_activation.router,
        prefix="/infra",
        dependencies=[Depends(auth_admin_key)],
    )
    client = TestClient(app)

    response = client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(),
    )
    assert response.status_code in {401, 403}
    assert orchestra_and_k8s_boundary["launch_calls"] == []


def test_provider_event_dispatch_status_is_not_found_for_unknown_operation(
    provider_event_client,
):
    response = provider_event_client.get(
        "/infra/task-activation/provider-event-dispatch/op-missing",
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "provider_event_dispatch_not_found"
