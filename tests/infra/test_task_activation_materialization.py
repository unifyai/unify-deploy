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
        self.missing_queue_names: set[str] = set()
        self.existing_task_names: set[str] = set()

    def get_queue(self, *, name):
        if not self.queue_exists or name.rsplit("/", 1)[-1] in self.missing_queue_names:
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

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    task_activation._task_queues_ensured = set()

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.adapters_url",
            "https://adapters.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
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
    assert parent.endswith(f"/queues/{task_activation.SETTINGS.task_due_queue_name}")
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
    assert task.schedule_time.seconds == int(
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert (
        task.dispatch_deadline.seconds
        == task_activation.SETTINGS.task_due_dispatch_deadline_seconds
    )


def test_upsert_scheduled_task_activation_deletes_previous_materialization(
    client,
    fake_tasks_module,
):
    """Changing the activation identity should delete the previous Cloud Task."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    previous_name = task_activation._scheduled_activation_task_name(
        assistant_id="assistant-123",
        task_id=101,
        activation_revision="rev-old",
        scheduled_for=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
    )
    fake_client.existing_task_names.add(previous_name)
    task_activation._task_queues_ensured = {
        task_activation.SETTINGS.task_due_queue_name,
    }

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.adapters_url",
            "https://adapters.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
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


def test_upsert_mode_change_deletes_previous_live_materialization_by_default(
    client,
    fake_tasks_module,
):
    """Mode-only updates should delete the prior live task when no previous mode is sent."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    previous_name = task_activation._scheduled_activation_task_name(
        assistant_id="assistant-123",
        task_id=101,
        activation_revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
        execution_mode="live",
    )
    fake_client.existing_task_names.add(previous_name)
    task_activation._task_queues_ensured = {
        task_activation.SETTINGS.task_due_queue_name,
    }

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.comms_url",
            "https://comms.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
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
                "execution_mode": "offline",
                "previous_activation_revision": "rev-123",
                "previous_scheduled_for": "2026-04-10T09:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["previous_deleted"] is True
    assert previous_name in fake_client.deleted_task_names


def test_delete_scheduled_task_activation_is_idempotent(client):
    """Deleting a missing materialization should still return success."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    task_activation._task_queues_ensured = set()

    with patch(
        "communication.infra.task_activation._get_cloud_tasks_client",
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


def test_upsert_offline_scheduled_task_activation_targets_offline_queue(
    client,
    fake_tasks_module,
):
    """Offline activations should materialize onto the hidden offline queue."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    task_activation._task_queues_ensured = set()

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.comms_url",
            "https://comms.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
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
                "execution_mode": "offline",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(
        f"/queues/{task_activation.SETTINGS.task_offline_queue_name}",
    )
    assert (
        task.http_request.url
        == "https://comms.test/infra/task-activation/offline-dispatch"
    )
    assert b'"execution_mode": "offline"' in task.http_request.body


def test_upsert_far_future_activation_targets_repair_queue(
    client,
    fake_tasks_module,
):
    """Far-future activations should chain through the repair queue horizon."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    task_activation._task_queues_ensured = set()
    now = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
    scheduled_for = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    expected_checkpoint = now + task_activation.timedelta(
        days=task_activation.SETTINGS.task_activation_horizon_days,
    )

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.comms_url",
            "https://comms.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
            return_value=fake_client,
        ),
        patch("communication.infra.task_activation.datetime") as mock_datetime,
        patch.dict(sys.modules, {"google.cloud.tasks_v2": fake_tasks_module}),
    ):
        mock_datetime.now.return_value = now
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        response = client.post(
            "/infra/task-activation/upsert",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
                "activation_revision": "rev-123",
                "scheduled_for": scheduled_for.isoformat(),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"
    assert body["scheduled_checkpoint_for"] == expected_checkpoint.isoformat()
    parent, task = fake_client.created_tasks[0]
    assert parent.endswith(
        f"/queues/{task_activation.SETTINGS.task_activation_repair_queue_name}",
    )
    assert task.http_request.url == "https://comms.test/infra/task-activation/repair"
    assert task.schedule_time.seconds == int(expected_checkpoint.timestamp())


def test_upsert_recreates_existing_activation_to_repair_drift(
    client,
    fake_tasks_module,
):
    """AlreadyExists should delete and recreate the Cloud Task instead of silently accepting drift."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    task_name = task_activation._scheduled_activation_task_name(
        assistant_id="assistant-123",
        task_id=101,
        activation_revision="rev-123",
        scheduled_for=datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )
    fake_client.existing_task_names.add(task_name)
    task_activation._task_queues_ensured = {
        task_activation.SETTINGS.task_due_queue_name,
    }

    with (
        patch(
            "communication.infra.task_activation.SETTINGS.orchestra_admin_key",
            "test-admin-key",
        ),
        patch(
            "communication.infra.task_activation.SETTINGS.adapters_url",
            "https://adapters.test",
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
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
    assert body["status"] == "recreated"
    assert task_name in fake_client.deleted_task_names


def test_validate_task_activation_infra_reports_required_queues(client):
    """Validation should report the live, offline, and repair queue status."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    fake_client.missing_queue_names.add(
        task_activation.SETTINGS.task_offline_queue_name,
    )

    with patch(
        "communication.infra.task_activation._get_cloud_tasks_client",
        return_value=fake_client,
    ):
        response = client.get("/infra/task-activation/validate")

    assert response.status_code == 200
    body = response.json()
    statuses = {item["queue_name"]: item["status"] for item in body["queues"]}
    assert statuses[task_activation.SETTINGS.task_due_queue_name] == "ok"
    assert statuses[task_activation.SETTINGS.task_offline_queue_name] == "missing"
    assert statuses[task_activation.SETTINGS.task_activation_repair_queue_name] == "ok"


def test_diagnose_task_activation_reports_materialization_and_latest_run(
    client,
):
    """Diagnosis should combine activation, Cloud Task target, queues, and latest run."""

    from communication.infra import task_activation

    fake_client = _FakeCloudTasksClient()
    activation = {
        "activation_kind": "scheduled",
        "execution_mode": "live",
        "activation_revision": "rev-123",
        "source_task_log_id": 555,
        "next_due_at": "2026-04-10T09:00:00+00:00",
    }
    latest_run = {"run_key": "live:scheduled:assistant-123:101:rev:once"}

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=activation,
        ),
        patch(
            "communication.infra.task_activation._lookup_latest_task_run",
            return_value=latest_run,
        ),
        patch(
            "communication.infra.task_activation._get_cloud_tasks_client",
            return_value=fake_client,
        ),
    ):
        response = client.post(
            "/infra/task-activation/diagnose",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["activation"] == activation
    assert body["latest_run"] == latest_run
    assert body["materialization"]["queue_name"] == (
        task_activation.SETTINGS.task_due_queue_name
    )
    assert body["materialization"]["target_url"].endswith("/scheduled/tasks/due")
    assert "task-live-assistant-123-101" in body["materialization"]["task_name"]
