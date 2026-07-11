"""Unit tests for the hidden offline task dispatch lane."""

import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from unittest.mock import AsyncMock, patch


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


def _assistant_data(**overrides):
    assistant_data = {
        "assistant_id": "assistant-123",
        "team_ids": [],
        "self_contact_id": 42,
        "boss_contact_id": 43,
    }
    assistant_data.update(overrides)
    return assistant_data


def _client() -> TestClient:
    from common.settings import SETTINGS
    from communication.infra.views import assistant_self_router, router

    # offline-dispatch is now a self-scoped route; send the admin key so the
    # admin short-circuit applies (these tests target dispatch logic, not auth).
    SETTINGS.orchestra_admin_key = "TEST-ADMIN-KEY"
    app = FastAPI()
    app.include_router(router, prefix="/infra")
    app.include_router(assistant_self_router, prefix="/infra")
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer TEST-ADMIN-KEY"})
    return test_client


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


def test_explicit_offline_dispatch_skips_orchestra_activation_lookup():
    """Explicit REST triggers must not re-fetch the activation Orchestra just resolved."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(
            source_type="explicit",
            scheduled_for=None,
            source_ref="req-rest-skip-lookup",
            source_medium="api",
            entrypoint=777,
            task_name="Daily summary",
            task_description="Send the daily summary email.",
        ),
    )

    with patch(
        "communication.infra.task_activation._lookup_current_task_activation",
    ) as mock_lookup:
        activation = task_activation._resolve_offline_dispatch_activation(request)

    mock_lookup.assert_not_called()
    assert activation is not None
    assert activation["activation_revision"] == "rev-123"
    assert activation["entrypoint"] == 777
    assert activation["source_task_log_id"] == 555
    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_scheduled_offline_dispatch_still_looks_up_current_activation():
    """Delayed scheduled deliveries still re-check the current Orchestra activation."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    current = _activation()

    with patch(
        "communication.infra.task_activation._lookup_current_task_activation",
        return_value=current,
    ) as mock_lookup:
        activation = task_activation._resolve_offline_dispatch_activation(request)

    mock_lookup.assert_called_once_with(
        assistant_id="assistant-123",
        task_id=101,
        destination=None,
    )
    assert activation is current


def test_offline_dispatch_launches_job_for_current_activation():
    """Valid offline deliveries should create/adopt a run and launch a headless job."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(),
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
    assert (
        mock_launch.call_args.kwargs["assistant_data"]["assistant_id"]
        == "assistant-123"
    )
    assert mock_update_run.call_count == 1
    create_payload = mock_create_run.call_args.args[0]
    assert create_payload["task_name"] == "Daily summary"
    assert create_payload["task_description"] == "Send the daily summary email."
    assert create_payload["entrypoint"] == 777
    update_kwargs = mock_update_run.call_args.kwargs
    assert update_kwargs["assistant_id"] == "assistant-123"
    assert update_kwargs["updates"]["state"] == "running"
    assert update_kwargs["updates"]["job_name"] == "unity-offline-abc"


def test_offline_dispatch_retries_failed_terminal_run():
    """Failed runs should be retryable without changing run identity."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={
                "run": {
                    "state": "failed",
                    "job_name": "unity-offline-old",
                    "error": "boom",
                    "retry_count": 1,
                },
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            return_value=("batch-api", None, None, None),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=("unity-offline-retry", True),
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
    assert response.json()["status"] == "launched"
    assert response.json()["run_key"] == mock_create_run.call_args.args[0]["run_key"]
    assert mock_launch.call_args.kwargs["job_name_seed"].endswith(":retry:2")
    update_kwargs = mock_update_run.call_args.kwargs
    assert update_kwargs["updates"]["state"] == "running"
    assert update_kwargs["updates"]["job_name"] == "unity-offline-retry"
    assert update_kwargs["updates"]["retry_count"] == 2
    assert update_kwargs["updates"]["previous_error"] == "boom"
    assert update_kwargs["updates"]["error"] is None


def test_offline_dispatch_retries_stale_inflight_run():
    """In-flight run rows with missing jobs should be failed before retry."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={
                "run": {
                    "state": "running",
                    "run_key": "offline:scheduled:assistant-123:101:rev-123",
                    "job_name": "unity-offline-missing",
                },
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            return_value=("batch-api", None, None, None),
        ),
        patch(
            "communication.infra.task_activation._classify_offline_job_status",
            return_value={"status": "missing", "job_name": "unity-offline-missing"},
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=("unity-offline-retry", True),
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
    assert response.json()["status"] == "launched"
    run_key = mock_create_run.call_args.args[0]["run_key"]
    assert response.json()["run_key"] == run_key
    assert mock_launch.call_args.kwargs["job_name_seed"].endswith(":retry:1")
    assert mock_update_run.call_count == 2
    failed_update = mock_update_run.call_args_list[0].kwargs["updates"]
    assert failed_update["state"] == "failed"
    assert "lost live execution evidence" in failed_update["error"]
    running_update = mock_update_run.call_args_list[1].kwargs["updates"]
    assert running_update["state"] == "running"
    assert running_update["job_name"] == "unity-offline-retry"
    assert running_update["retry_count"] == 1


def test_diagnose_classifies_missing_job_run_as_stale():
    """Diagnosis should report repairable stale runs when their job is gone."""

    client = _client()
    diagnostic = {
        "success": True,
        "assistant_id": "assistant-123",
        "task_id": 101,
        "activation": _activation(
            next_due_at="2026-04-10T09:00:00+00:00",
        ),
        "materialization": {"cloud_task_status": "missing"},
        "queues": {},
        "latest_run": {
            "run_key": "offline:scheduled:assistant-123:101:rev-123",
            "state": "running",
            "execution_mode": "offline",
            "job_name": "unity-offline-missing",
            "source_task_log_id": 555,
            "activation_revision": "rev-123",
            "scheduled_for": "2026-04-10T09:00:00+00:00",
        },
        "health": {"status": "fired_inflight", "repairable": False},
    }

    with (
        patch(
            "communication.infra.task_activation._activation_materialization_diagnostic",
            return_value=diagnostic,
        ),
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            return_value=("batch-api", None, None, None),
        ),
        patch(
            "communication.infra.task_activation._classify_offline_job_status",
            return_value={"status": "missing", "job_name": "unity-offline-missing"},
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
    assert body["latest_run_job"]["status"] == "missing"
    assert body["health"]["status"] == "stale_running_run"
    assert body["health"]["repairable"] is True
    assert body["blocking_conditions"][0]["type"] == "stale_running_run"


def test_task_activation_health_summarizes_blocking_conditions():
    """Health endpoint should return alertable lifecycle counters."""

    client = _client()
    diagnostic = {
        "success": True,
        "assistant_id": "assistant-123",
        "task_id": 101,
        "activation": _activation(),
        "materialization": {"cloud_task_status": "missing"},
        "queues": {},
        "latest_run": {"state": "running"},
        "health": {"status": "stale_running_run", "repairable": True},
        "blocking_conditions": [{"type": "stale_running_run"}],
    }

    with patch(
        "communication.infra.task_activation.diagnose_task_activation",
        new=AsyncMock(return_value=diagnostic),
    ):
        response = client.post(
            "/infra/task-activation/health",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["total"] == 1
    assert summary["repairable"] == 1
    assert summary["statuses"] == {"stale_running_run": 1}
    assert summary["blocking_conditions"] == {"stale_running_run": 1}
    assert summary["job_lifecycle_safeguards"]["backoff_limit"] == 0
    assert summary["job_lifecycle_safeguards"]["active_deadline_seconds"] >= 1800
    assert summary["job_lifecycle_safeguards"]["ttl_seconds_after_finished"] > 0


def test_reconcile_current_repairs_only_repairable_diagnostics():
    """Reconcile should call repair-current only for deterministic stale states."""

    client = _client()
    diagnostic = {
        "success": True,
        "activation": _activation(),
        "health": {"status": "stale_running_run", "repairable": True},
        "blocking_conditions": [{"type": "stale_running_run"}],
    }
    repair = {"success": True, "status": "retry_dispatched"}

    with (
        patch(
            "communication.infra.task_activation.diagnose_task_activation",
            new=AsyncMock(return_value=diagnostic),
        ),
        patch(
            "communication.infra.task_activation.repair_current_task_activation",
            new=AsyncMock(return_value=repair),
        ) as mock_repair,
    ):
        response = client.post(
            "/infra/task-activation/reconcile-current",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "reconciled"
    assert response.json()["repair"] == repair
    assert mock_repair.await_count == 1


def test_reconcile_current_noops_non_repairable_diagnostics():
    """Reconcile should not mutate healthy or ambiguous activation states."""

    client = _client()
    diagnostic = {
        "success": True,
        "activation": _activation(),
        "health": {"status": "armed_future", "repairable": False},
    }

    with (
        patch(
            "communication.infra.task_activation.diagnose_task_activation",
            new=AsyncMock(return_value=diagnostic),
        ),
        patch(
            "communication.infra.task_activation.repair_current_task_activation",
            new=AsyncMock(),
        ) as mock_repair,
    ):
        response = client.post(
            "/infra/task-activation/reconcile-current",
            json={
                "assistant_id": "assistant-123",
                "task_id": 101,
                "source_task_log_id": 555,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "noop"
    assert response.json()["reason"] == "armed_future"
    assert mock_repair.await_count == 0


def test_offline_dispatch_adopts_completed_terminal_run():
    """Completed runs should remain terminal and should not relaunch."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={
                "run": {"state": "completed", "job_name": "unity-offline-old"},
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
        ) as mock_launch,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "adopted_terminal_run",
        "run_key": mock_create_run.call_args.args[0]["run_key"],
        "run_state": "completed",
    }
    assert not mock_launch.called


def test_repair_current_retries_fired_failed_activation():
    """Repair should dispatch failed fired offline activations."""

    client = _client()
    activation = _activation(
        assistant_id="assistant-123",
        task_id=101,
        next_due_at="2026-04-10T09:00:00+00:00",
    )
    diagnostic = {
        "success": True,
        "activation": activation,
        "materialization": {"cloud_task_status": "missing"},
        "latest_run": {"state": "failed"},
        "health": {"status": "fired_failed_retryable", "repairable": True},
    }

    with (
        patch(
            "communication.infra.task_activation._activation_materialization_diagnostic",
            return_value=diagnostic,
        ),
        patch(
            "communication.infra.task_activation.dispatch_offline_task",
            new=AsyncMock(return_value={"success": True, "status": "launched"}),
        ) as mock_dispatch,
    ):
        response = client.post(
            "/infra/task-activation/repair-current",
            json={"assistant_id": "assistant-123", "task_id": 101},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "retry_dispatched"
    dispatched_request = mock_dispatch.call_args.args[0]
    assert dispatched_request.execution_mode == "offline"
    assert dispatched_request.entrypoint == 777


def test_repair_current_reprojects_completed_activation():
    """Completed runs should repair by reprojection, not duplicate dispatch."""

    client = _client()
    activation = _activation(
        assistant_id="assistant-123",
        task_id=101,
        next_due_at="2026-04-10T09:00:00+00:00",
    )
    diagnostic = {
        "success": True,
        "activation": activation,
        "materialization": {"cloud_task_status": "missing"},
        "latest_run": {"state": "completed"},
        "health": {"status": "completed_not_rearmed", "repairable": True},
    }

    with (
        patch(
            "communication.infra.task_activation._activation_materialization_diagnostic",
            return_value=diagnostic,
        ),
        patch(
            "communication.infra.task_activation._reproject_task_activation",
            return_value={"upserted": 1, "deleted": 0},
        ) as mock_reproject,
        patch(
            "communication.infra.task_activation.dispatch_offline_task",
            new=AsyncMock(return_value={"success": True, "status": "launched"}),
        ) as mock_dispatch,
    ):
        response = client.post(
            "/infra/task-activation/repair-current",
            json={"assistant_id": "assistant-123", "task_id": 101},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "reprojected"
    assert mock_reproject.call_args.kwargs == {
        "assistant_id": "assistant-123",
        "task_id": 101,
    }
    assert not mock_dispatch.called


def test_offline_dispatch_allows_agentic_activation_without_entrypoint():
    """Offline delivery should not imply symbolic function execution."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    activation = _activation(entrypoint=None)

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_explicit_offline_dispatch_accepts_scheduled_activation():
    """REST explicit triggers may fire scheduled offline activations."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(
            source_type="explicit",
            scheduled_for=None,
            source_ref="req-rest-1",
            source_medium="api",
        ),
    )
    activation = _activation(activation_kind="scheduled")

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_explicit_offline_dispatch_accepts_triggered_activation():
    """REST explicit triggers may also fire triggered offline activations."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(
            source_type="explicit",
            scheduled_for=None,
            source_ref="req-rest-2",
        ),
    )
    activation = _activation(
        activation_kind="triggered",
        next_due_at=None,
    )

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_triggered_offline_dispatch_rejects_scheduled_activation_kind():
    """Inbound triggered dispatch still requires activation_kind=triggered."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="triggered", scheduled_for=None),
    )
    activation = _activation(activation_kind="scheduled")

    assert (
        task_activation._validate_current_offline_activation(request, activation)
        == "activation_kind_changed"
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
        assistant_data=_assistant_data(api_key="key"),
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
        assistant_data=_assistant_data(api_key="key"),
        run_key="offline:scheduled:assistant-123:101:rev:once",
        job_name="unity-offline-abc",
    )

    assert env["UNITY_OFFLINE_TASK_MODE"] == "actor"
    assert env["UNITY_OFFLINE_TASK_FUNCTION_ID"] == "777"


def test_offline_dispatch_persists_authorized_destination_on_run_create():
    """Authorized shared offline dispatch should carry destination into the run row."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(destination="team:7"),
        ) as mock_lookup,
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(team_ids=[7]),
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
            json=_payload(destination="team:7"),
        )

    assert response.status_code == 200
    create_payload = mock_create_run.call_args.args[0]
    assert create_payload["destination"] == "team:7"
    assert create_payload["run_key"].startswith(
        "offline:scheduled:assistant-123:team-7:101:",
    )
    assert mock_lookup.call_args.kwargs["destination"] == "team:7"


def test_offline_dispatch_skips_revoked_team_destination():
    """Offline dispatch should ack shared activations after membership revocation."""

    client = _client()

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(destination="team:7"),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(team_ids=[8]),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
        ) as mock_launch,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(destination="team:7"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "skipped",
        "reason": "destination_membership_revoked",
    }
    mock_create_run.assert_not_called()
    mock_launch.assert_not_called()


def test_offline_runner_env_marks_assistant_as_non_coordinator():
    """Headless task execution should never inherit the Coordinator role."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    env = task_activation._build_offline_runner_env(
        request=request,
        activation=_activation(),
        assistant_data=_assistant_data(api_key="test-api-key", is_coordinator=True),
        run_key="offline:scheduled:assistant-123:101",
        job_name="unity-offline-abc",
    )

    assert "ASSISTANT_IS_COORDINATOR" not in env


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

    request.destination = "team:7"
    assert task_activation._build_offline_run_key(request) == (
        f"offline:triggered:assistant-123:team-7:101:{revision_digest}:"
        f"contact-77-sms-message-{source_ref_digest}"
    )


def test_offline_runner_env_carries_team_ids_as_csv():
    """Headless task runs receive membership ids through the env bridge."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())

    env = task_activation._build_offline_runner_env(
        request=request,
        activation=_activation(),
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "team_ids": [1, 2],
            "team_summaries": [
                {
                    "team_id": 1,
                    "name": "Ops",
                    "description": "Operations workspace for customer support.",
                },
            ],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="run-123",
        job_name="unity-offline-abc",
    )

    assert env["TEAM_IDS"] == "1,2"
    assert json.loads(env["TEAM_SUMMARIES"]) == [
        {
            "team_id": 1,
            "name": "Ops",
            "description": "Operations workspace for customer support.",
        },
    ]
    assert env["SELF_CONTACT_ID"] == "42"
    assert env["BOSS_CONTACT_ID"] == "43"
    assert "TASK_DESTINATION" not in env


def test_offline_runner_env_carries_task_destination():
    """Shared offline task runs receive the destination for routed writes."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(destination="team:7"),
    )

    env = task_activation._build_offline_runner_env(
        request=request,
        activation=_activation(destination="team:7"),
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "team_ids": [7],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="run-123",
        job_name="unity-offline-abc",
    )

    assert env["TASK_DESTINATION"] == "team:7"


def test_offline_runner_env_uses_empty_team_ids_for_solo_assistant():
    """Solo assistants keep the env value present but empty."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())

    env = task_activation._build_offline_runner_env(
        request=request,
        activation=_activation(),
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "team_ids": [],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="run-123",
        job_name="unity-offline-abc",
    )

    assert env["TEAM_IDS"] == ""
    assert env["TEAM_SUMMARIES"] == ""


def test_offline_runner_env_requires_resolved_contact_ids():
    """Offline jobs fail before launching if assistant identity is incomplete."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())

    with pytest.raises(RuntimeError, match="self_contact_id"):
        task_activation._build_offline_runner_env(
            request=request,
            activation=_activation(),
            assistant_data={"assistant_id": "assistant-123", "api_key": "test-api-key"},
            run_key="run-123",
            job_name="unity-offline-abc",
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
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(),
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
