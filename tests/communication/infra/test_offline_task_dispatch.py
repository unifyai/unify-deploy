"""Unit tests for offline task dispatch as dedicated one-shot Kubernetes Jobs."""

import hashlib
import json
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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


@pytest.fixture(autouse=True)
def _default_no_latest_run_for_source_flight():
    """Avoid Orchestra HTTP from source single-flight checks in unit tests."""

    with patch(
        "communication.infra.task_activation._lookup_latest_task_run",
        return_value=None,
    ):
        yield


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


def test_offline_task_job_name_is_deterministic_and_retry_salted():
    """Job names must be a stable function of run_key, salted per retry."""

    from communication.infra import task_activation

    run_key = "offline:scheduled:assistant-123:101:abc123def456:once"
    first = task_activation._build_offline_task_job_name(run_key)
    second = task_activation._build_offline_task_job_name(run_key)
    retry = task_activation._build_offline_task_job_name(run_key, retry_count=1)

    assert first == second
    assert first.startswith("unity-task-run-")
    assert retry != first
    assert retry.startswith("unity-task-run-")


def test_launch_offline_task_job_builds_one_shot_manifest():
    """The Job is a self-contained one-shot; runner env rides a per-run Secret."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    batch_api = MagicMock()
    core_api = MagicMock()

    created = task_activation._launch_offline_task_job(
        batch_api=batch_api,
        core_api=core_api,
        request=request,
        run_key="offline:scheduled:assistant-123:101:abc123def456:once",
        job_name="unity-task-run-abc123def456",
        offline_env={
            "UNITY_OFFLINE_TASK_MODE": "actor",
            "ASSISTANT_ID": "123",
            "UNIFY_KEY": "secret-key",
        },
        max_runtime_seconds=None,
    )

    assert created is True
    manifest = batch_api.create_namespaced_job.call_args.kwargs["body"]
    assert manifest["metadata"]["name"] == "unity-task-run-abc123def456"
    assert manifest["metadata"]["labels"]["app"] == "unity-task-run"
    assert manifest["metadata"]["labels"]["unity-status"] == "offline"
    assert manifest["metadata"]["labels"]["task-id"] == "101"
    assert manifest["metadata"]["annotations"]["unify.ai/task-run-key"] == (
        "offline:scheduled:assistant-123:101:abc123def456:once"
    )
    assert (
        manifest["spec"]["backoffLimit"]
        == task_activation.OFFLINE_TASK_JOB_BACKOFF_LIMIT
    )
    assert manifest["spec"]["backoffLimit"] > 0
    assert "ttlSecondsAfterFinished" in manifest["spec"]
    # No per-task bound means the run is unbounded: no activeDeadlineSeconds.
    assert "activeDeadlineSeconds" not in manifest["spec"]
    # Offline Jobs need grace above SmartLead's 60s HTTP timeout so SIGTERM
    # writeback can finish before kubelet SIGKILLs the runner.
    assert (
        manifest["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        == task_activation.OFFLINE_TASK_TERMINATION_GRACE_PERIOD_SECONDS
    )
    assert task_activation.OFFLINE_TASK_TERMINATION_GRACE_PERIOD_SECONDS > 60

    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [
        {"secretRef": {"name": "unity-task-run-abc123def456"}},
    ]
    # Credentials must never appear inline in the pod spec.
    inline_env_names = {var["name"] for var in container["env"]}
    assert "UNIFY_KEY" not in inline_env_names
    assert "UNITY_OFFLINE_TASK_MODE" not in inline_env_names

    secret_body = core_api.create_namespaced_secret.call_args.kwargs["body"]
    assert secret_body.metadata.name == "unity-task-run-abc123def456"
    assert secret_body.string_data["UNIFY_KEY"] == "secret-key"
    assert secret_body.string_data["UNITY_OFFLINE_TASK_MODE"] == "actor"

    # The Job adopts the Secret so both garbage-collect together.
    owner_patch = core_api.patch_namespaced_secret.call_args.kwargs["body"]
    owner_refs = owner_patch["metadata"]["ownerReferences"]
    assert owner_refs[0]["kind"] == "Job"


def test_launch_offline_task_job_adopts_existing_job_with_matching_run_key():
    """A 409 with the same run-key annotation is treated as adopt-not-create."""

    from kubernetes.client.rest import ApiException

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    batch_api = MagicMock()
    core_api = MagicMock()
    batch_api.create_namespaced_job.side_effect = ApiException(status=409)
    batch_api.read_namespaced_job.return_value = {
        "metadata": {
            "annotations": {"unify.ai/task-run-key": "rk"},
        },
    }

    created = task_activation._launch_offline_task_job(
        batch_api=batch_api,
        core_api=core_api,
        request=request,
        run_key="rk",
        job_name="unity-task-run-abc123def456",
        offline_env={},
        max_runtime_seconds=None,
    )

    assert created is False
    batch_api.read_namespaced_job.assert_called_once_with(
        name="unity-task-run-abc123def456",
        namespace=task_activation.SETTINGS.default_namespace,
    )
    core_api.patch_namespaced_secret.assert_not_called()


def test_launch_offline_task_job_rejects_name_conflict_with_mismatched_run_key():
    """A 409 whose existing Job carries a different run key fails closed."""

    from kubernetes.client.rest import ApiException

    from communication.infra import task_activation
    from communication.infra.provider_event_dispatch import (
        ProviderEventDispatchValidationError,
    )

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    batch_api = MagicMock()
    core_api = MagicMock()
    batch_api.create_namespaced_job.side_effect = ApiException(status=409)
    batch_api.read_namespaced_job.return_value = {
        "metadata": {
            "annotations": {"unify.ai/task-run-key": "other-run-key"},
        },
    }

    with pytest.raises(ProviderEventDispatchValidationError) as exc:
        task_activation._launch_offline_task_job(
            batch_api=batch_api,
            core_api=core_api,
            request=request,
            run_key="rk",
            job_name="unity-task-run-abc123def456",
            offline_env={},
            max_runtime_seconds=None,
        )
    assert exc.value.reason_code == "offline_job_run_key_mismatch"
    core_api.patch_namespaced_secret.assert_not_called()


def test_launch_offline_task_job_applies_per_task_runtime_bound():
    """A task's max_runtime_seconds becomes the Job's activeDeadlineSeconds."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(**_payload())
    batch_api = MagicMock()
    core_api = MagicMock()

    task_activation._launch_offline_task_job(
        batch_api=batch_api,
        core_api=core_api,
        request=request,
        run_key="rk",
        job_name="unity-task-run-abc123def456",
        offline_env={},
        max_runtime_seconds=7200,
    )

    manifest = batch_api.create_namespaced_job.call_args.kwargs["body"]
    assert manifest["spec"]["activeDeadlineSeconds"] == 7200


def test_offline_dispatch_launches_job_for_current_activation():
    """Valid offline deliveries should create/adopt a run and launch a one-shot Job."""

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
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
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
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "launched_job"
    run_key = mock_create_run.call_args.args[0]["run_key"]
    assert body["run_key"] == run_key
    from communication.infra.task_activation import _build_offline_task_job_name

    assert body["job_name"] == _build_offline_task_job_name(run_key)
    assert mock_launch.call_args.kwargs["run_key"] == run_key
    offline_env = mock_launch.call_args.kwargs["offline_env"]
    assert offline_env["UNITY_OFFLINE_TASK_RUN_KEY"] == run_key
    assert offline_env["UNITY_OFFLINE_TASK_JOB_NAME"] == body["job_name"]
    assert mock_update_run.call_count == 1
    create_payload = mock_create_run.call_args.args[0]
    assert create_payload["task_name"] == "Daily summary"
    assert create_payload["task_description"] == "Send the daily summary email."
    assert create_payload["entrypoint"] == 777
    update_kwargs = mock_update_run.call_args.kwargs
    assert update_kwargs["assistant_id"] == "assistant-123"
    assert update_kwargs["updates"]["state"] == "running"
    assert update_kwargs["updates"]["job_name"] == body["job_name"]


def test_offline_dispatch_reports_already_dispatched_on_job_conflict():
    """A concurrent delivery that lost the Job-name race must not double-run."""

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
        ),
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=False,
        ),
        patch(
            "communication.infra.task_activation._update_task_run",
        ) as mock_update_run,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "already_dispatched"
    mock_update_run.assert_not_called()


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
                    "job_name": "unity-task-run-old",
                    "error": "boom",
                    "retry_count": 1,
                },
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
        ) as mock_launch,
        patch(
            "communication.infra.task_activation._update_task_run",
        ) as mock_update_run,
        patch(
            "communication.infra.task_activation._release_active_task_source",
            return_value={"updated": True, "mode": "reopen"},
        ) as mock_release,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "launched_job"
    run_key = mock_create_run.call_args.args[0]["run_key"]
    assert response.json()["run_key"] == run_key
    from communication.infra.task_activation import _build_offline_task_job_name

    expected_job_name = _build_offline_task_job_name(run_key, retry_count=2)
    assert response.json()["job_name"] == expected_job_name
    offline_env = mock_launch.call_args.kwargs["offline_env"]
    assert offline_env["UNITY_OFFLINE_TASK_JOB_NAME"] == expected_job_name
    update_kwargs = mock_update_run.call_args.kwargs
    assert update_kwargs["updates"]["state"] == "running"
    assert update_kwargs["updates"]["job_name"] == expected_job_name
    assert update_kwargs["updates"]["retry_count"] == 2
    assert update_kwargs["updates"]["previous_error"] == "boom"
    assert update_kwargs["updates"]["error"] is None
    mock_release.assert_called_once()
    assert mock_release.call_args.kwargs["mode"] == "reopen"


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
                    "job_name": "unity-task-run-missing",
                },
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._classify_offline_job_status",
            return_value={"status": "missing", "job_name": "unity-task-run-missing"},
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
        ) as mock_launch,
        patch(
            "communication.infra.task_activation._update_task_run",
        ) as mock_update_run,
        patch(
            "communication.infra.task_activation._release_active_task_source",
            return_value={"updated": True, "mode": "reopen"},
        ) as mock_release,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "launched_job"
    run_key = mock_create_run.call_args.args[0]["run_key"]
    assert response.json()["run_key"] == run_key
    from communication.infra.task_activation import _build_offline_task_job_name

    expected_job_name = _build_offline_task_job_name(run_key, retry_count=1)
    offline_env = mock_launch.call_args.kwargs["offline_env"]
    assert offline_env["UNITY_OFFLINE_TASK_JOB_NAME"] == expected_job_name
    assert mock_update_run.call_count == 2
    failed_update = mock_update_run.call_args_list[0].kwargs["updates"]
    assert failed_update["state"] == "failed"
    assert "lost live execution evidence" in failed_update["error"]
    running_update = mock_update_run.call_args_list[1].kwargs["updates"]
    assert running_update["state"] == "running"
    assert running_update["job_name"] == expected_job_name
    assert running_update["retry_count"] == 1
    mock_release.assert_called_once()
    assert mock_release.call_args.kwargs["mode"] == "reopen"
    assert mock_release.call_args.kwargs["source_task_log_id"] == 555


def test_offline_dispatch_adopts_inflight_source_for_different_run_key():
    """A second dispatch must not launch when another Job owns the Tasks source."""

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
                    "state": "pending",
                    "run_key": "offline:explicit:assistant-123:101:ref-new",
                },
                "created": True,
            },
        ),
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._adopt_inflight_source_conflict",
            return_value={
                "success": True,
                "status": "adopted_inflight_source",
                "run_key": "offline:scheduled:assistant-123:101:rev-123",
                "job_name": "unity-task-run-owner",
                "run_state": "running",
                "job_status": {"status": "active"},
                "source_task_log_id": 555,
            },
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
        ) as mock_launch,
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(source_type="explicit", source_ref="ref-new"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "adopted_inflight_source"
    assert body["job_name"] == "unity-task-run-owner"
    mock_launch.assert_not_called()


def test_adopt_inflight_source_conflict_detects_active_foreign_job():
    from communication.infra import task_activation as mod
    from communication.infra.models import OfflineTaskDispatchRequest

    request = OfflineTaskDispatchRequest(
        **_payload(source_type="explicit", source_ref="b"),
    )
    with (
        patch(
            "communication.infra.task_activation._lookup_latest_task_run",
            return_value={
                "run_key": "offline:scheduled:assistant-123:101:rev-123",
                "state": "running",
                "job_name": "unity-task-run-owner",
            },
        ),
        patch(
            "communication.infra.task_activation._classify_offline_job_status",
            return_value={"status": "active", "job_name": "unity-task-run-owner"},
        ),
    ):
        conflict = mod._adopt_inflight_source_conflict(
            batch_api=object(),
            request=request,
            exclude_run_key="offline:explicit:assistant-123:101:b",
        )

    assert conflict is not None
    assert conflict["status"] == "adopted_inflight_source"
    assert conflict["job_name"] == "unity-task-run-owner"


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
    assert summary["job_lifecycle_safeguards"]["backoff_limit"] == 2
    assert summary["job_lifecycle_safeguards"]["active_deadline_seconds"] == (
        "per-task max_runtime_seconds (None = unbounded)"
    )
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
                "run": {"state": "completed", "job_name": "unity-task-run-old"},
                "created": False,
            },
        ) as mock_create_run,
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
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
            new=AsyncMock(return_value={"success": True, "status": "launched_job"}),
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
            new=AsyncMock(return_value={"success": True, "status": "launched_job"}),
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
        job_name="unity-assistant-abc",
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
        job_name="unity-assistant-abc",
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
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
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
        job_name="unity-assistant-abc",
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
        job_name="unity-assistant-abc",
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
    # No owner_team_id in assistant data → env present but empty (solo scope).
    assert env["OWNER_TEAM_ID"] == ""


def test_offline_runner_env_carries_owner_team_id_for_team_owned():
    """Team-owned assistants must propagate owner_team_id so shared-scoped
    tables (Data, Tasks, …) resolve to Teams/{owner}/… instead of the
    personal root."""

    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(destination="team:11"),
    )

    env = task_activation._build_offline_runner_env(
        request=request,
        activation=_activation(destination="team:11"),
        assistant_data={
            "assistant_id": "1406",
            "api_key": "test-api-key",
            "team_ids": [11],
            "owner_team_id": 11,
            "self_contact_id": 0,
            "boss_contact_id": 1,
        },
        run_key="run-123",
        job_name="unity-assistant-abc",
    )

    assert env["OWNER_TEAM_ID"] == "11"


def test_offline_runner_env_rejects_invalid_owner_team_id():
    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(destination="team:11"),
    )
    with pytest.raises(RuntimeError, match="invalid owner_team_id"):
        task_activation._build_offline_runner_env(
            request=request,
            activation=_activation(destination="team:11"),
            assistant_data={
                "assistant_id": "1406",
                "api_key": "test-api-key",
                "owner_team_id": "not-an-int",
                "self_contact_id": 0,
                "boss_contact_id": 1,
            },
            run_key="run-123",
            job_name="unity-assistant-abc",
        )


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
        job_name="unity-assistant-abc",
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
        job_name="unity-assistant-abc",
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
            job_name="unity-assistant-abc",
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
            new=AsyncMock(return_value=("batch-api", None, None, None)),
        ),
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
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


def test_offline_dispatch_request_accepts_provider_event_source_type():
    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="provider_event", scheduled_for=None),
    )
    assert request.source_type == "provider_event"


def test_offline_dispatch_request_accepts_triggered_and_explicit():
    from communication.infra import task_activation

    explicit = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="explicit", scheduled_for=None),
    )
    triggered = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="triggered", scheduled_for=None),
    )
    assert explicit.source_type == "explicit"
    assert triggered.source_type == "triggered"


def test_explicit_offline_dispatch_accepts_scheduled_activation():
    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="explicit", scheduled_for=None),
    )
    activation = _activation(activation_kind="scheduled")
    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_triggered_offline_dispatch_requires_triggered_activation_kind():
    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="triggered", scheduled_for=None),
    )
    activation = _activation(activation_kind="scheduled")
    assert (
        task_activation._validate_current_offline_activation(request, activation)
        == "activation_kind_changed"
    )


def test_provider_event_offline_dispatch_requires_provider_event_activation_kind():
    from communication.infra import task_activation

    request = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="provider_event", scheduled_for=None),
    )
    activation = _activation(activation_kind="provider_event", next_due_at=None)
    assert (
        task_activation._validate_current_offline_activation(request, activation)
        is None
    )


def test_legacy_triggered_and_explicit_still_pass_kind_matching():
    from communication.infra import task_activation

    legacy_triggered = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="triggered", scheduled_for=None),
    )
    legacy_explicit = task_activation.OfflineTaskDispatchRequest(
        **_payload(source_type="explicit", scheduled_for=None),
    )
    triggered_activation = _activation(activation_kind="triggered", next_due_at=None)
    scheduled_activation = _activation(activation_kind="scheduled")

    assert (
        task_activation._validate_current_offline_activation(
            legacy_triggered,
            triggered_activation,
        )
        is None
    )
    assert (
        task_activation._validate_current_offline_activation(
            legacy_explicit,
            scheduled_activation,
        )
        is None
    )


def test_resolve_resource_flags_merges_request_and_activation():
    """Request and activation requires_* flags merge with OR semantics."""
    from communication.infra import task_activation

    assert task_activation._resolve_resource_flags({}) == (False, False)
    assert task_activation._resolve_resource_flags(
        {"requires_filesystem": True},
    ) == (True, False)
    assert task_activation._resolve_resource_flags(
        {"requires_computer": True},
    ) == (False, True)
    assert task_activation._resolve_resource_flags(
        {},
        request_requires_computer=True,
    ) == (False, True)
    assert task_activation._resolve_resource_flags(
        {"requires_filesystem": False},
        request_requires_filesystem=True,
        request_requires_computer=True,
    ) == (True, True)


def test_assistant_desktop_browser_env_resolves_ready_binding():
    """Desktop-targeted workers receive only the current ready binding URL."""
    from communication.infra import task_activation

    session = {
        "spec": {"desiredState": "Running"},
        "status": {
            "conditions": [{"type": "DesktopReady", "status": "True"}],
            "binding": {"desktopUrl": "https://assistant-123.vm.unify.ai"},
        },
    }
    with (
        patch(
            "communication.infra.task_activation.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.task_activation.get_assistant_session",
            return_value=session,
        ),
    ):
        env = asyncio.run(
            task_activation._assistant_desktop_browser_env(
                "assistant-123",
                assistant_data=_assistant_data(
                    desktop_mode="ubuntu",
                    managed_desktop_status="active",
                ),
            ),
        )

    assert env == {
        "ASSISTANT_BROWSER_TARGET": "assistant_desktop",
        "ASSISTANT_DESKTOP_URL": "https://assistant-123.vm.unify.ai",
        "ASSISTANT_ID": "assistant-123",
    }


def test_requires_computer_offline_dispatch_resolves_desktop_binding():
    """requires_computer=True takes the desktop-ready gate before launch."""

    client = _client()
    desktop_env = {
        "ASSISTANT_BROWSER_TARGET": "assistant_desktop",
        "ASSISTANT_DESKTOP_URL": "https://assistant-123.vm.unify.ai",
        "ASSISTANT_ID": "assistant-123",
    }

    with (
        patch(
            "communication.infra.task_activation._lookup_current_task_activation",
            return_value=_activation(),
        ),
        patch(
            "communication.infra.task_activation._get_assistant_data",
            return_value=_assistant_data(api_key="key"),
        ),
        patch(
            "communication.infra.task_activation._create_or_adopt_task_run",
            return_value={"run": {"state": "pending"}, "created": True},
        ),
        patch(
            "communication.infra.task_activation._get_k8s_clients",
            new=AsyncMock(return_value=("batch-api", "core-api", None, None)),
        ),
        patch(
            "communication.infra.task_activation._assistant_desktop_browser_env",
            new=AsyncMock(return_value=desktop_env),
        ) as mock_desktop,
        patch(
            "communication.infra.task_activation._launch_offline_task_job",
            return_value=True,
        ) as mock_launch,
        patch("communication.infra.task_activation._update_task_run"),
    ):
        response = client.post(
            "/infra/task-activation/offline-dispatch",
            json=_payload(requires_computer=True),
        )

    assert response.status_code == 200
    mock_desktop.assert_awaited_once()
    offline_env = mock_launch.call_args.kwargs["offline_env"]
    assert offline_env["ASSISTANT_DESKTOP_URL"] == desktop_env["ASSISTANT_DESKTOP_URL"]
    assert offline_env["UNITY_OFFLINE_TASK_REQUIRES_COMPUTER"] == "1"


def test_assistant_desktop_browser_env_uses_local_worker_without_computer_use():
    """Desktop-eligible tasks retain the normal worker browser when disabled."""
    from communication.infra import task_activation

    assert (
        asyncio.run(
            task_activation._assistant_desktop_browser_env(
                "assistant-123",
                assistant_data=_assistant_data(
                    desktop_mode="none",
                    managed_desktop_status="disabled",
                ),
            ),
        )
        == {}
    )
