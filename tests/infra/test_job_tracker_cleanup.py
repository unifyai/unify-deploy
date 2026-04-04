from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.infra.integration import conftest as integration_conftest


def _job(
    *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels=labels or {},
            annotations=annotations or {},
        ),
    )


def test_job_tracker_deletes_synthetic_assistant_labeled_jobs_directly(monkeypatch):
    batch_api = MagicMock()
    batch_api.read_namespaced_job.return_value = _job(
        labels={"assistant-id": "test-guard", "unity-status": "running"},
    )
    stop_runtime = MagicMock()
    replenish_pool = MagicMock()
    tracker = integration_conftest.JobTracker(
        jobs=["unity-job-1"],
        batch_api=batch_api,
    )

    monkeypatch.setattr(
        integration_conftest,
        "stop_assistant_runtime",
        stop_runtime,
    )
    monkeypatch.setattr(
        integration_conftest,
        "replenish_pool",
        replenish_pool,
    )

    tracker.cleanup()

    batch_api.delete_namespaced_job.assert_called_once_with(
        name="unity-job-1",
        namespace=integration_conftest.NAMESPACE,
        propagation_policy="Foreground",
    )
    stop_runtime.assert_not_called()
    replenish_pool.assert_called_once()


def test_job_tracker_uses_session_driven_cleanup_for_session_backed_jobs(monkeypatch):
    batch_api = MagicMock()
    batch_api.read_namespaced_job.return_value = _job(
        labels={
            "assistant-id": "1207",
            integration_conftest.ASSISTANT_SESSION_REF_LABEL: "assistant-session-1207",
            "unity-status": "running",
        },
    )
    stop_runtime = MagicMock()
    replenish_pool = MagicMock()
    tracker = integration_conftest.JobTracker(
        jobs=["unity-job-1"],
        batch_api=batch_api,
    )

    monkeypatch.setattr(
        integration_conftest,
        "stop_assistant_runtime",
        stop_runtime,
    )
    monkeypatch.setattr(
        integration_conftest,
        "replenish_pool",
        replenish_pool,
    )

    tracker.cleanup()

    batch_api.delete_namespaced_job.assert_not_called()
    stop_runtime.assert_called_once_with(
        "1207",
        batch_api=batch_api,
        timeout=180,
    )
    replenish_pool.assert_called_once()
