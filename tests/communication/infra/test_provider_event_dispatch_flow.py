"""Offline provider-event dispatch request validation through the Comms route."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from communication.dependencies import auth_admin_key
from communication.infra import task_activation
from communication.infra.provider_event_dispatch import offline_launch_identity


def _dispatch_payload(**overrides) -> dict:
    payload = {
        "contract_version": "1",
        "operation_id": "op-flow-1",
        "run_id": 4242,
        "run_key": (
            "offline:provider_event:assistant-123:101:binding-1:rev123:"
            "abcdef0123456789"
        ),
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


@pytest.fixture
def provider_event_client(monkeypatch):
    from common.settings import SETTINGS

    SETTINGS.orchestra_admin_key = "TEST-ADMIN-KEY"
    app = FastAPI()
    app.include_router(
        task_activation.router,
        prefix="/infra",
        dependencies=[Depends(auth_admin_key)],
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return client


def test_offline_launch_identity_uses_deterministic_job_name() -> None:
    assert (
        offline_launch_identity(
            run_key="offline:provider_event:a:1:b:r:digest",
            job_name="unity-task-run-abcdef012345",
        )
        == "unity-task-run-abcdef012345"
    )


def test_provider_event_dispatch_rejects_invalid_audience_and_mode(
    provider_event_client,
) -> None:
    bad_audience = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(audience="unity:provider-event-dispatch"),
    )
    assert bad_audience.status_code == 400
    assert bad_audience.json()["detail"]["reason"] == "invalid_audience"

    bad_mode = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(dispatch_mode="live"),
    )
    assert bad_mode.status_code == 400
    assert bad_mode.json()["detail"]["reason"] == "invalid_dispatch_mode"


def test_provider_event_dispatch_rejects_expired_request(
    provider_event_client,
) -> None:
    response = provider_event_client.post(
        "/infra/task-activation/provider-event-dispatch",
        json=_dispatch_payload(
            issued_at=datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        ),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "dispatch_request_expired"


def test_provider_event_status_route_removed(provider_event_client) -> None:
    response = provider_event_client.get(
        "/infra/task-activation/provider-event-dispatch/op-flow-1",
    )
    assert response.status_code == 404
