"""Unit tests for AssistantJobs infra write proxies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from communication.dependencies import CallerContext
from communication.infra.self_router import assistant_self_router
import communication.infra.assistant_jobs_routes as routes


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        routes.SETTINGS,
        "orchestra_url",
        "https://orchestra.test/v0",
    )
    monkeypatch.setattr(
        routes.SETTINGS,
        "orchestra_admin_key",
        "test-admin-key",
    )
    app = FastAPI()
    app.include_router(assistant_self_router, prefix="/infra")
    return TestClient(app)


def test_startup_route_writes_via_admin_key(client: TestClient) -> None:
    auth = AsyncMock(return_value=CallerContext(is_admin=False, identity=None))
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"log_event_ids": [99]}
    context_resp = MagicMock()
    context_resp.status_code = 400
    context_resp.text = "exists"

    with (
        patch.object(routes, "authorize_admin_or_assistant", auth),
        patch.object(
            routes.requests,
            "post",
            side_effect=[context_resp, fake_resp],
        ) as post,
    ):
        response = client.post(
            "/infra/assistant-jobs/startup",
            json={
                "assistant_id": "42",
                "user_id": "user-1",
                "job_name": "job-abc",
                "medium": "chat",
            },
            headers={"Authorization": "Bearer assistant-key"},
        )

    assert response.status_code == 200
    assert response.json()["log_event_ids"] == [99]
    auth.assert_awaited_once()
    assert post.call_count == 2
    log_call = post.call_args_list[1]
    assert log_call.args[0].endswith("/logs")
    assert log_call.kwargs["headers"]["Authorization"] == "Bearer test-admin-key"
    assert log_call.kwargs["json"]["project_name"] == "AssistantJobs"
    assert log_call.kwargs["json"]["entries"]["assistant_id"] == "42"


def test_liveview_route_updates_existing_row(client: TestClient) -> None:
    auth = AsyncMock(return_value=CallerContext(is_admin=True))
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {"logs": [{"id": 7}]}
    put_resp = MagicMock()
    put_resp.status_code = 200
    put_resp.json.return_value = {"info": "ok"}

    with (
        patch.object(routes, "authorize_admin_or_assistant", auth),
        patch.object(routes.requests, "get", return_value=get_resp),
        patch.object(routes.requests, "put", return_value=put_resp) as put,
    ):
        response = client.patch(
            "/infra/assistant-jobs/liveview",
            json={
                "assistant_id": "42",
                "job_name": "job-abc",
                "liveview_url": "https://vm.example/desktop",
            },
            headers={"Authorization": "Bearer admin"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "log_id": 7,
        "liveview_url": "https://vm.example/desktop",
    }
    put.assert_called_once()
    assert put.call_args.kwargs["json"]["logs"] == [7]
    assert put.call_args.kwargs["json"]["entries"]["liveview_url"] == (
        "https://vm.example/desktop"
    )
