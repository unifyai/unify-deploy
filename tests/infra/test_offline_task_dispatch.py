"""Unit tests for the hidden offline task dispatch lane."""

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch


def _payload(**overrides):
    payload = {
        "assistant_id": "assistant-123",
        "task_id": 101,
        "source_task_log_id": 555,
        "activation_revision": "rev-123",
        "execution_mode": "offline",
        "source_type": "scheduled",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _activation(**overrides):
    activation = {
        "activation_kind": "scheduled",
        "execution_mode": "offline",
        "activation_revision": "rev-123",
        "source_task_log_id": 555,
        "entrypoint": 777,
        "next_due_at": "2026-04-10T09:00:00+00:00",
        "task_name": "Daily summary",
        "task_description": "Send the daily summary email.",
    }
    activation.update(overrides)
    return activation


def _client() -> TestClient:
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def test_offline_dispatch_skips_stale_activation():
    """Stale deliveries must not launch headless Unity jobs."""

    client = _client()

    with patch(
        "communication.infra.task_activation._lookup_current_task_activation",
        return_value=_activation(activation_revision="rev-new"),
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "skipped",
        "reason": "activation_revision_mismatch",
    }


def test_offline_dispatch_launches_job_for_current_activation():
    """Valid offline deliveries should create/adopt a run and launch a headless job."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={"run": {"state": "pending"}, "created": True},
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            return_value=("batch-api", None, None, None),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=("unity-offline-abc", True),
        ) as mock_launch,
        patch(
            "communication.infra.task_activation._update_task_run",
        ) as mock_update_run,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "launched",
        "run_key": mock_create_run.call_args.args[0]["run_key"],
        "job_name": "unity-offline-abc",
    }
    assert mock_launch.called
    assert mock_update_run.call_count == 1
    create_payload = mock_create_run.call_args.args[0]
    assert create_payload["task_name"] == "Daily summary"
    assert create_payload["task_description"] == "Send the daily summary email."
    assert create_payload["entrypoint"] == 777
    update_kwargs = mock_update_run.call_args.kwargs
    assert update_kwargs["assistant_id"] == "assistant-123"
    assert update_kwargs["updates"]["state"] == "running"
    assert update_kwargs["updates"]["job_name"] == "unity-offline-abc"


def test_offline_dispatch_allows_agentic_activation_without_entrypoint():
    """Offline delivery should not imply symbolic function execution."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    activation = _activation(entrypoint=None)

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_offline_dispatch_rejects_stale_request_entrypoint():
    """A request cannot claim a function when the current activation is agentic."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(entrypoint=777),
    )
    activation = _activation(entrypoint=None)

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        == "entrypoint_mismatch"
    )


def test_offline_runner_env_carries_agentic_execution_without_function_id():
    """The Unity job payload should preserve agentic offline execution."""

    from communication.infra import task_activation

    env = task_activation._build_offline_runner_env(
        request=task_activation.OfflineTaskDispatchRequest(**_payload()),
        activation=_activation(entrypoint=None),
        assistant_data={"assistant_id": "assistant-123", "api_key": "key"},
        run_key="offline:scheduled:assistant-123:101:rev:once",
        job_name="unity-offline-abc",
    )

    assert env["UNITY_OFFLINE_TASK_MODE"] == "actor"
    assert env["UNITY_OFFLINE_TASK_FUNCTION_ID"] == ""
    assert env["UNITY_OFFLINE_TASK_REQUEST"] == "Send the daily summary email."


def test_offline_runner_env_carries_symbolic_function_id():
    """The Unity job payload should preserve symbolic offline execution."""

    from communication.infra import task_activation

    env = task_activation._build_offline_runner_env(
        request=task_activation.OfflineTaskDispatchRequest(**_payload()),
        activation=_activation(entrypoint=777),
        assistant_data={"assistant_id": "assistant-123", "api_key": "key"},
        run_key="offline:scheduled:assistant-123:101:rev:once",
        job_name="unity-offline-abc",
    )

    assert env["UNITY_OFFLINE_TASK_MODE"] == "actor"
    assert env["UNITY_OFFLINE_TASK_FUNCTION_ID"] == "777"


def test_offline_run_key_uses_canonical_trigger_provenance_shape():
    """Triggered offline runs should use the same provenance ingredients as live."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        assistant_id="assistant-123",
        task_id=101,
        source_task_log_id=555,
        activation_revision="rev-123",
        execution_mode="offline",
        source_type="triggered",
        source_medium="sms_message",
        source_ref="message-123",
        source_contact_id="77",
    )
    revision_digest = hashlib.sha256(b"rev-123").hexdigest()[:12]
    source_ref_digest = hashlib.sha256(b"message-123").hexdigest()[:12]

    assert task_activation._build_offline_run_key(request) == (
        f"offline:triggered:assistant-123:101:{revision_digest}:"
        f"contact-77-sms-message-{source_ref_digest}"
    )


def test_offline_dispatch_persists_trigger_provenance_on_run_create():
    """Triggered offline dispatch should persist the known provenance fields."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(
                activation_kind="triggered",
                next_due_at=None,
            ),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={"run": {"state": "pending"}, "created": True},
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            return_value=("batch-api", None, None, None),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=("unity-offline-abc", True),
        ),
        patch("communication.infra.task_activation._update_task_run"),
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(
                source_type="triggered",
                scheduled_for=None,
                source_medium="whatsapp",
                source_ref="message-123",
                source_contact_id="77",
                source_contact_display_name="Alice Owner",
            ),
        )

    assert response.status_code == 200
    create_payload = mock_create_run.call_args.args[0]
    assert create_payload["source_medium"] == "whatsapp"
    assert create_payload["source_ref"] == "message-123"
    assert create_payload["source_contact_id"] == "77"
    assert create_payload["source_contact_display_name"] == "Alice Owner"
    assert create_payload["task_name"] == "Daily summary"
    assert create_payload["task_description"] == "Send the daily summary email."
