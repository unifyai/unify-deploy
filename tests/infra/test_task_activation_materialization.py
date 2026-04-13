"""Unit tests for scheduled task activation materialization endpoints."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound as GcpNotFound
import pytest


class _FakeHttpMethod:
    POST = "POST"


class _FakeHttpRequest:
    def __init__(self, *, http_method, url, headers, body):
        self.http_method = http_method
        self.url = url
        self.headers = headers
        self.body = body


class _FakeTask:
    def __init__(self, *, name, http_request, schedule_time, dispatch_deadline):
        self.name = name
        self.http_request = http_request
        self.schedule_time = schedule_time
        self.dispatch_deadline = dispatch_deadline


class _FakeQueue:
    def __init__(self, *, name):
        self.name = name


class _FakeCloudTasksClient:
    def __init__(self):
        self.created_queues = []
        self.created_tasks = []
        self.deleted_task_names = []
        self.queue_exists = True
        self.existing_task_names: set[str] = set()

    def get_queue(self, *, name):
        if not self.queue_exists:
            raise GcpNotFound("queue missing")
        return {"name": name}

    def create_queue(self, *, parent, queue):
        self.queue_exists = True
        self.created_queues.append((parent, queue))
        return {"name": queue.name}

    def create_task(self, *, parent, task):
        if task.name in self.existing_task_names:
            from google.api_core.exceptions import AlreadyExists

            raise AlreadyExists("duplicate task")
        self.existing_task_names.add(task.name)
        self.created_tasks.append((parent, task))
        return {"name": task.name}

    def delete_task(self, *, name):
        if name not in self.existing_task_names:
            raise GcpNotFound("task missing")
        self.deleted_task_names.append(name)
        self.existing_task_names.remove(name)


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


@pytest.fixture
def fake_tasks_module():
    return SimpleNamespace(
        HttpMethod=_FakeHttpMethod,
        HttpRequest=_FakeHttpRequest,
        Task=_FakeTask,
        Queue=_FakeQueue,
    )


def test_upsert_scheduled_task_activation_creates_cloud_task(client, fake_tasks_module):
    """Upsert should create one Cloud Task with the adapters due callback."""

    from communication.infra import views

    fake_client = _FakeCloudTasksClient()
    views._task_due_queue_ensured = False
    views._cloud_tasks_client = fake_client

    with (
        patch(
            "communication.infra.views.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.views.SETTINGS.adapters_url",
            "https://adapters.test",
        ),
        patch(
            "communication.infra.views._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-activation/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "activation_revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "created"
    assert len(fake_client.created_tasks) == 1

    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(f"/queues/{views.SETTINGS.task_due_queue_name}")
    assert task.http_request.url.endswith("/scheduled/tasks/due")
    assert task.http_request.headers["Authorization"].startswith("Bearer ")
    assert b'"task_id": 101' in task.http_request.body
    assert task.schedule_time.seconds == int(
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert (
        task.dispatch_deadline.seconds
        == views.SETTINGS.task_due_dispatch_deadline_seconds
    )


def test_upsert_scheduled_task_activation_deletes_previous_materialization(
    client,
    fake_tasks_module,
):
    """Changing the activation identity should delete the previous Cloud Task."""

    from communication.infra import views

    fake_client = _FakeCloudTasksClient()
    previous_name = views._scheduled_activation_task_name(
        assistant_id="assistant-123",
        task_id=101,
        activation_revision="rev-old",
        scheduled_for=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
    )
    fake_client.existing_task_names.add(previous_name)
    views._task_due_queue_ensured = True
    views._cloud_tasks_client = fake_client

    with (
        patch(
            "communication.infra.views.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.views.SETTINGS.adapters_url",
            "https://adapters.test",
        ),
        patch(
            "communication.infra.views._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-activation/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "activation_revision": "rev-new",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "previous_activation_revision": "rev-old",
                "previous_scheduled_for": "2026-04-10T08:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["previous_deleted"] is True
    assert previous_name in fake_client.deleted_task_names


def test_delete_scheduled_task_activation_is_idempotent(client):
    """Deleting a missing materialization should still return success."""

    from communication.infra import views

    fake_client = _FakeCloudTasksClient()
    views._cloud_tasks_client = fake_client

    with patch(
        "communication.infra.views._get_cloud_tasks_client",
        return_value=fake_client,
    ):
        response = client.post(
            "/infra/task-activation/delete",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "activation_revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["deleted"] is False
