"""Unit tests for scheduled task activation materialization endpoints."""

import os

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import (
    NotFound as GcpNotFound,
    PermissionDenied as GcpPermissionDenied,
)
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
        self.get_task_names = []
        self.queue_exists = True
        self.missing_queue_names: set[str] = set()
        self.permission_denied_queue_names: set[str] = set()
        self.existing_task_names: set[str] = set()
        self.permission_denied_task_names: set[str] = set()
        self.create_without_persisting_task_names: set[str] = set()

    def get_queue(self, *, name):
        if name.rsplit("/", 1)[-1] in self.permission_denied_queue_names:
            raise GcpPermissionDenied("queue permission denied")
        if not self.queue_exists or name.rsplit("/", 1)[-1] in self.missing_queue_names:
            raise GcpNotFound("queue missing")
        return {"name": name}

    def get_task(self, *, name):
        self.get_task_names.append(name)
        if name in self.permission_denied_task_names:
            raise GcpPermissionDenied("task permission denied")
        if name not in self.existing_task_names:
            raise GcpNotFound("task missing")
        return {"name": name}

    def create_queue(self, *, parent, queue):
        self.queue_exists = True
        self.created_queues.append((parent, queue))
        return {"name": queue.name}

    def create_task(self, *, parent, task):
        if task.name in self.existing_task_names:
            from google.api_core.exceptions import AlreadyExists

            raise AlreadyExists("duplicate task")
        if task.name not in self.create_without_persisting_task_names:
            self.existing_task_names.add(task.name)
        self.created_tasks.append((parent, task))
        return {"name": task.name}

    def delete_task(self, *, name):
        if name not in self.existing_task_names:
            raise GcpNotFound("task missing")
        self.deleted_task_names.append(name)
        self.existing_task_names.remove(name)


@pytest.fixture
def client(monkeypatch):
    from communication.infra.views import assistant_self_router, router

    # offline-dispatch is now a self-scoped route; send the admin key so the
    # admin short-circuit applies (these tests target materialization, not auth).
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "TEST-ADMIN-KEY")
    app = FastAPI()
    app.include_router(router, prefix="/infra")
    app.include_router(assistant_self_router, prefix="/infra")
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return test_client


@pytest.fixture
def fake_tasks_module():
    return SimpleNamespace(
        HttpMethod=_FakeHttpMethod,
        HttpRequest=_FakeHttpRequest,
        Task=_FakeTask,
        Queue=_FakeQueue,
    )


def test_upsert_scheduled_task_execution_creates_cloud_task(client, fake_tasks_module):
    """Upsert should create one Cloud Task with the adapters due callback."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "task_label": "Morning briefing",
                "task_summary": "Prepare the morning update before the user checks in.",
                "visibility_policy": "silent_by_default",
                "recurrence_hint": "recurring",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "created"
    assert len(fake_client.created_tasks) == 1

    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(f"/queues/{task_execution.SETTINGS.task_due_queue_name}")
    assert task.http_request.url.endswith("/scheduled/tasks/due")
    assert task.http_request.headers["Authorization"].startswith("Bearer ")
    assert b'"task_id": 101' in task.http_request.body
    assert b'"task_label": "Morning briefing"' in task.http_request.body
    assert (
        b'"task_summary": "Prepare the morning update before the user checks in."'
        in task.http_request.body
    )
    assert b'"visibility_policy": "silent_by_default"' in task.http_request.body
    assert b'"recurrence_hint": "recurring"' in task.http_request.body
    assert b'"destination"' not in task.http_request.body
    assert task.schedule_time.seconds == int(
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert (
        task.dispatch_deadline.seconds
        == task_execution.SETTINGS.task_due_dispatch_deadline_seconds
    )
    assert fake_client.get_task_names == [task.name]


def test_upsert_live_symbolic_activation_carries_entrypoint(
    client,
    fake_tasks_module,
):
    """Symbolic live activations should carry the function entrypoint."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "delivery": "live",
                "entrypoint": 777,
            },
        )

    assert response.status_code == 200
    _, task = fake_client.created_tasks[0]
    assert task.http_request.url == "https://adapters.test/scheduled/tasks/due"
    assert b'"delivery": "live"' in task.http_request.body
    assert b'"entrypoint": 777' in task.http_request.body


def test_upsert_scheduled_task_execution_threads_destination(
    client,
    fake_tasks_module,
):
    """Shared task activations should carry their destination to due delivery."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "destination": "team:7",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    _, task = fake_client.created_tasks[0]
    assert b'"destination": "team:7"' in task.http_request.body


def test_upsert_scheduled_task_execution_deletes_previous_materialization(
    client,
    fake_tasks_module,
):
    """Changing the activation identity should delete the previous Cloud Task."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    previous_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-old",
        scheduled_for=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
    )
    fake_client.existing_task_names.add(previous_name)
    task_execution._task_queues_ensured = {
        task_execution.SETTINGS.task_due_queue_name,
    }

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-new",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "previous_revision": "rev-old",
                "previous_scheduled_for": "2026-04-10T08:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["previous_deleted"] is True
    assert previous_name in fake_client.deleted_task_names


def test_upsert_mode_change_deletes_previous_live_materialization_by_default(
    client,
    fake_tasks_module,
):
    """Mode-only updates should delete the prior live task when no previous mode is sent."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    previous_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
        delivery="live",
    )
    fake_client.existing_task_names.add(previous_name)
    task_execution._task_queues_ensured = {
        task_execution.SETTINGS.task_due_queue_name,
    }

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_COMMS_URL": "https://comms.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "delivery": "offline",
                "previous_revision": "rev-123",
                "previous_scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["previous_deleted"] is True
    assert previous_name in fake_client.deleted_task_names


def test_delete_scheduled_task_execution_is_idempotent(client):
    """Deleting a missing materialization should still return success."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with patch(
        "communication.infra.task_execution._get_cloud_tasks_client",
        return_value=fake_client,
    ):
        response = client.post(
            "/infra/task-execution/delete",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["deleted"] is False


def test_upsert_offline_scheduled_task_execution_targets_offline_queue(
    client,
    fake_tasks_module,
):
    """Offline activations should materialize onto the hidden offline queue."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_COMMS_URL": "https://comms.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "delivery": "offline",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(
        f"/queues/{task_execution.SETTINGS.task_offline_queue_name}",
    )
    assert (
        task.http_request.url
        == "https://comms.test/infra/task-execution/offline-dispatch"
    )
    assert b'"delivery": "offline"' in task.http_request.body
    assert b'"entrypoint": null' in task.http_request.body


def test_upsert_offline_symbolic_activation_carries_entrypoint(
    client,
    fake_tasks_module,
):
    """Symbolic offline activations should carry the function entrypoint."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_COMMS_URL": "https://comms.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
                "delivery": "offline",
                "entrypoint": 777,
            },
        )

    assert response.status_code == 200
    _, task = fake_client.created_tasks[0]
    assert b'"delivery": "offline"' in task.http_request.body
    assert b'"entrypoint": 777' in task.http_request.body


def test_upsert_far_future_activation_targets_repair_queue(
    client,
    fake_tasks_module,
):
    """Far-future activations should chain through the repair queue horizon."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()
    now = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
    scheduled_for = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    expected_checkpoint = now + task_execution.timedelta(
        days=task_execution.SETTINGS.task_execution_horizon_days,
    )

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_COMMS_URL": "https://comms.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch("communication.infra.task_execution.datetime") as mock_datetime,
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        mock_datetime.now.return_value = now
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": scheduled_for.isoformat(),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    assert body["scheduled_checkpoint_for"] == expected_checkpoint.isoformat()
    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(
        f"/queues/{task_execution.SETTINGS.task_execution_repair_queue_name}",
    )
    assert task.http_request.url == "https://comms.test/infra/task-execution/repair"
    assert task.schedule_time.seconds == int(expected_checkpoint.timestamp())


def test_upsert_far_future_duplicate_is_non_destructive(
    client,
    fake_tasks_module,
):
    """Duplicate far-future repair materialization should preserve the checkpoint."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    now = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
    scheduled_for = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    task_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-123",
        scheduled_for=scheduled_for,
        queue_name=task_execution.SETTINGS.task_execution_repair_queue_name,
    )
    fake_client.existing_task_names.add(task_name)
    task_execution._task_queues_ensured = {
        task_execution.SETTINGS.task_execution_repair_queue_name,
    }

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_COMMS_URL": "https://comms.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch("communication.infra.task_execution.datetime") as mock_datetime,
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        mock_datetime.now.return_value = now
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": scheduled_for.isoformat(),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_exists"
    assert fake_client.deleted_task_names == []
    assert fake_client.created_tasks == []
    assert fake_client.get_task_names == [task_name]


def test_upsert_existing_activation_is_non_destructive(
    client,
    fake_tasks_module,
):
    """Duplicate materialization should preserve the existing Cloud Task."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )
    fake_client.existing_task_names.add(task_name)
    task_execution._task_queues_ensured = {
        task_execution.SETTINGS.task_due_queue_name,
    }

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_exists"
    assert fake_client.deleted_task_names == []
    assert fake_client.created_tasks == []
    assert fake_client.get_task_names == [task_name]
    assert task_name in fake_client.existing_task_names


def test_upsert_fails_when_materialization_cannot_be_verified(
    client,
    fake_tasks_module,
):
    """Upsert should fail if the created Cloud Task cannot be read back."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )
    fake_client.create_without_persisting_task_names.add(task_name)
    task_execution._task_queues_ensured = {
        task_execution.SETTINGS.task_due_queue_name,
    }

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 500
    assert task_name in fake_client.get_task_names


def test_validate_task_execution_infra_reports_required_queues(client):
    """Validation should report the live, offline, and repair queue status."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    fake_client.missing_queue_names.add(
        task_execution.SETTINGS.task_offline_queue_name,
    )

    with patch(
        "communication.infra.task_execution._get_cloud_tasks_client",
        return_value=fake_client,
    ):
        response = client.get("/infra/task-execution/validate")

    assert response.status_code == 200
    body = response.json()
    statuses = {item["queue_name"]: item["status"] for item in body["queues"]}
    assert statuses[task_execution.SETTINGS.task_due_queue_name] == "ok"
    assert statuses[task_execution.SETTINGS.task_offline_queue_name] == "missing"
    assert statuses[task_execution.SETTINGS.task_execution_repair_queue_name] == "ok"


def test_validate_task_execution_infra_reports_queue_permission_denied(client):
    """Validation should expose Cloud Tasks IAM failures per queue."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    fake_client.permission_denied_queue_names.add(
        task_execution.SETTINGS.task_due_queue_name,
    )

    with patch(
        "communication.infra.task_execution._get_cloud_tasks_client",
        return_value=fake_client,
    ):
        response = client.get("/infra/task-execution/validate")

    assert response.status_code == 200
    body = response.json()
    statuses = {item["queue_name"]: item["status"] for item in body["queues"]}
    assert statuses[task_execution.SETTINGS.task_due_queue_name] == "permission_denied"


def test_diagnose_task_execution_reports_materialization_and_latest_run(
    client,
):
    """Diagnosis should combine activation, Cloud Task target, queues, and latest run."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    execution = {
        "wake": "scheduled",
        "delivery": "live",
        "revision": "rev-123",
        "source_task_log_id": 555,
        "scheduled_for": "2026-04-10T09:00:00+00:00",
    }
    latest_run = {"run_key": "live:scheduled:assistant-123:101:rev:once"}

    with (
        patch(
            "communication.infra.task_execution._lookup_current_task_execution",
            return_value=execution,
        ),
        patch(
            "communication.infra.task_execution._lookup_latest_task_run",
            return_value=latest_run,
        ),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
    ):
        response = client.post(
            "/infra/task-execution/diagnose",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["execution"] == execution
    assert body["latest_run"] == latest_run
    assert body["materialization"]["queue_name"] == (
        task_execution.SETTINGS.task_due_queue_name
    )
    assert body["materialization"]["target_url"].endswith("/scheduled/tasks/due")
    assert "task-live-assistant-123-101" in body["materialization"]["task_name"]
    assert body["materialization"]["cloud_task_status"] == "missing"


def test_diagnose_task_execution_reports_cloud_task_permission_denied(client):
    """Diagnosis should identify Cloud Tasks IAM failures for the expected task."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    activation = {
        "wake": "scheduled",
        "delivery": "live",
        "revision": "rev-123",
        "source_task_log_id": 555,
        "scheduled_for": "2026-04-10T09:00:00+00:00",
    }
    task_name = task_execution._scheduled_execution_task_name(
        assistant_id="assistant-123",
        task_id=101,
        revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )
    fake_client.permission_denied_task_names.add(task_name)

    with (
        patch(
            "communication.infra.task_execution._lookup_current_task_execution",
            return_value=activation,
        ),
        patch(
            "communication.infra.task_execution._lookup_latest_task_run",
            return_value=None,
        ),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
    ):
        response = client.post(
            "/infra/task-execution/diagnose",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    materialization = response.json()["materialization"]
    assert materialization["task_name"] == task_name
    assert materialization["cloud_task_status"] == "permission_denied"


def test_execution_health_classifies_future_missing_materialization():
    """Future activations with no Cloud Task should be repairable."""

    from communication.infra import task_execution

    execution = {
        "wake": "scheduled",
        "scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }

    health = task_execution._execution_health(
        execution=execution,
        materialization={"cloud_task_status": "missing"},
        latest_run=None,
    )

    assert health["status"] == "stale_missing_materialization"
    assert health["repairable"] is True


def test_execution_health_classifies_fired_failed_activation():
    """Past failed activations should be retryable when the run identity matches."""

    from communication.infra import task_execution

    scheduled_for = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    execution = {
        "wake": "scheduled",
        "source_task_log_id": 555,
        "revision": "rev-123",
        "scheduled_for": scheduled_for,
    }
    latest_run = {
        "state": "failed",
        "source_task_log_id": 555,
        "revision": "rev-123",
        "scheduled_for": scheduled_for,
    }

    health = task_execution._execution_health(
        execution=execution,
        materialization={"cloud_task_status": "missing"},
        latest_run=latest_run,
    )

    assert health["status"] == "fired_failed_retryable"
    assert health["repairable"] is True


def test_repair_scheduled_task_execution_materializes_cloud_task(
    client,
    fake_tasks_module,
):
    """Repair endpoint should share the same materialization path as upsert."""

    from communication.infra import task_execution

    fake_client = _FakeCloudTasksClient()
    task_execution._task_queues_ensured = set()

    with (
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
        patch.dict(os.environ, {"UNITY_ADAPTERS_URL": "https://adapters.test"}),
        patch(
            "communication.infra.task_execution._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        response = client.post(
            "/infra/task-execution/repair",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "created"
    assert len(fake_client.created_tasks) == 1


def test_shared_get_assistant_fetches_orchestra_admin_directly(monkeypatch):
    """Assistant lookup should be usable without importing the adapters package."""

    from common import assistant_lookup

    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "info": [
                    {
                        "agent_id": "assistant-123",
                        "user_id": "user-123",
                        "api_key": "assistant-api-key",
                        "user_first_name": "Test",
                        "user_last_name": "User",
                        "first_name": "Test",
                        "surname": "Assistant",
                        "age": 30,
                        "nationality": "GB",
                        "about": "Assistant profile",
                        "job_title": "Researcher",
                        "timezone": "Europe/London",
                        "phone": None,
                        "assistant_whatsapp_number": None,
                        "email": "assistant@example.com",
                        "email_provider": "google_workspace",
                        "user_phone": None,
                        "user_whatsapp_number": None,
                        "user_email": "user@example.com",
                        "voice_provider": "cartesia",
                        "voice_id": "voice-123",
                        "secrets": {},
                        "desktop_mode": None,
                        "team_ids": [1, 2],
                        "organization_id": 42,
                    },
                ],
            }

    def get(url, *, params, headers, timeout=None):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            },
        )
        return Response()

    monkeypatch.setenv("ORCHESTRA_URL", "https://api.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(assistant_lookup.requests, "get", get)

    assistant_data = assistant_lookup.get_assistant(assistant_id="assistant-123")

    assert calls == [
        {
            "url": "https://api.test/admin/assistant",
            "params": {"agent_id": "assistant-123"},
            "headers": {"Authorization": "Bearer admin-key"},
            "timeout": assistant_lookup.ASSISTANT_LOOKUP_TIMEOUT_SECONDS,
        },
    ]
    assert assistant_data["assistant_id"] == "assistant-123"
    assert assistant_data["api_key"] == "assistant-api-key"
    assert assistant_data["desktop_mode"] == "none"
    assert assistant_data["team_ids"] == [1, 2]


def test_offline_dispatch_uses_shared_assistant_lookup(monkeypatch):
    """Offline dispatch should resolve assistant data through the shared helper."""

    from communication.infra import task_execution

    calls = []

    def get_assistant(*, assistant_id):
        calls.append(assistant_id)
        return {"assistant_id": assistant_id}

    monkeypatch.setattr(task_execution, "get_assistant", get_assistant)

    assert task_execution._get_assistant_data("assistant-123") == {
        "assistant_id": "assistant-123",
    }
    assert calls == ["assistant-123"]


def test_offline_dispatch_failure_logs_failing_stage(client, caplog):
    """Offline dispatch failures should identify the failed external operation."""

    import requests

    activation = {
        "wake": "scheduled",
        "delivery": "offline",
        "revision": "rev-123",
        "source_task_log_id": 555,
        "scheduled_for": "2026-04-10T09:00:00+00:00",
        "entrypoint": 6,
    }
    caplog.set_level(logging.INFO, logger="communication.infra.task_execution")

    with (
        patch(
            "communication.infra.task_execution._lookup_current_task_execution",
            return_value=activation,
        ),
        patch(
            "communication.infra.task_execution._get_assistant_data",
            return_value={
                "assistant_id": "assistant-123",
                "team_ids": [],
                "self_contact_id": 42,
                "boss_contact_id": 43,
            },
        ),
        patch(
            "communication.infra.task_execution._create_or_adopt_task_run",
            side_effect=requests.Timeout("orchestra timed out"),
        ),
    ):
        response = client.post(
            "/infra/task-execution/offline-dispatch",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "revision": "rev-123",
                "delivery": "offline",
                "wake": "scheduled",
                "scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 502
    events = [
        json.loads(record.message.removeprefix("OBS_EVENT "))
        for record in caplog.records
        if record.message.startswith("OBS_EVENT ")
    ]
    failed_event = next(
        event
        for event in events
        if event["event"] == "task_execution.offline_dispatch.failed"
    )
    assert failed_event["stage"] == "run_create_or_adopt"
    assert failed_event["error_type"] == "Timeout"
    assert failed_event["assistant_id"] == "assistant-123"
    assert failed_event["task_id"] == 101
