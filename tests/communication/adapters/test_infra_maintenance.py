import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"


@pytest.fixture(scope="module")
def app_module():
    # Share the already-loaded adapters.main; see test_api_message.py
    # for why wiping sys.modules here breaks other test files.
    from adapters import main

    return main


@pytest.fixture
def client(app_module, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    test_client = TestClient(app_module.app)
    test_client.headers["Authorization"] = "Bearer test-admin-key"
    return test_client


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def test_infra_maintenance_calls_terminal_session_prune(client, app_module):
    def _post(url: str, *_args, **_kwargs):
        if url.endswith("/infra/sessions/prune-terminal"):
            return _FakeResponse(
                {
                    "deleted_count": 2,
                    "deleted_assistant_ids": ["100", "101"],
                },
            )
        return _FakeResponse({"ok": True})

    with (
        patch.object(app_module, "SUPPORTED_POOL_VM_TYPES", ("ubuntu",)),
        patch.object(app_module, "replenish_idle_pool", return_value={"ok": True}),
        patch.object(app_module, "cleanup_idle_pool", return_value={"deleted": 0}),
        patch.object(app_module, "expire_all_stale_jobs", return_value={"expired": 0}),
        patch.object(app_module.requests, "post", side_effect=_post) as mock_post,
    ):
        response = client.post("/scheduled/infra/maintenance")

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_session_prune"] == {
        "deleted_count": 2,
        "deleted_assistant_ids": ["100", "101"],
    }
    assert any(
        call.args[0].endswith("/infra/sessions/prune-terminal")
        for call in mock_post.call_args_list
    )


def test_scheduled_jobs_create_passes_extra_demand(client, app_module):
    with patch.object(
        app_module,
        "replenish_idle_pool",
        return_value={"status": "ok"},
    ) as mock_replenish:
        response = client.post("/scheduled/jobs/create", params={"extra_demand": 2})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_replenish.assert_called_once_with(refresh=False, extra_demand=2)


class TestClassifyStaleRunning:
    """Fate decision for stale running jobs (pure helper, real inputs)."""

    @staticmethod
    def _job(name: str, assistant_id: str) -> dict:
        return {"job_name": name, "assistant_id": assistant_id}

    def test_session_on_live_call_is_deferred_not_stopped(self):
        """A bound, healthy, on-call session must never be torn down mid-call."""
        from adapters.helpers import classify_stale_running

        job = self._job("unity-job-2693", "2693")
        to_stop, safe_delete, deferred = classify_stale_running(
            [job],
            {
                "2693": {
                    "bound_job": "unity-job-2693",
                    "desired_state": "Running",
                    "terminal": False,
                    "has_active_call": True,
                },
            },
        )
        assert to_stop == {}
        assert safe_delete == []
        assert deferred == ["unity-job-2693"]

    def test_bound_idle_session_is_stopped(self):
        """The same session with no live call is the stop candidate."""
        from adapters.helpers import classify_stale_running

        job = self._job("unity-job-2693", "2693")
        to_stop, safe_delete, deferred = classify_stale_running(
            [job],
            {
                "2693": {
                    "bound_job": "unity-job-2693",
                    "desired_state": "Running",
                    "terminal": False,
                    "has_active_call": False,
                },
            },
        )
        assert to_stop == {"2693": "unity-job-2693"}
        assert safe_delete == []

    def test_already_stopping_session_is_deferred(self):
        from adapters.helpers import classify_stale_running

        job = self._job("unity-job-5", "5")
        to_stop, safe_delete, deferred = classify_stale_running(
            [job],
            {
                "5": {
                    "bound_job": "unity-job-5",
                    "desired_state": "Stopped",
                    "terminal": False,
                    "has_active_call": False,
                },
            },
        )
        assert to_stop == {}
        assert deferred == ["unity-job-5"]

    def test_orphaned_and_terminal_jobs_are_safe_deleted(self):
        from adapters.helpers import classify_stale_running

        jobs = [
            self._job("unity-job-missing", "10"),
            self._job("unity-job-terminal", "11"),
            self._job("unity-job-rebound", "12"),
            self._job("unity-job-noid", "unknown"),
        ]
        to_stop, safe_delete, deferred = classify_stale_running(
            jobs,
            {
                "10": {"missing": True},
                "11": {
                    "bound_job": "unity-job-terminal",
                    "terminal": True,
                    "has_active_call": False,
                },
                "12": {
                    "bound_job": "unity-job-current",
                    "terminal": False,
                    "has_active_call": False,
                },
            },
        )
        assert to_stop == {}
        assert set(safe_delete) == {
            "unity-job-missing",
            "unity-job-terminal",
            "unity-job-rebound",
            "unity-job-noid",
        }

    def test_inspection_failure_is_deferred(self):
        from adapters.helpers import classify_stale_running

        job = self._job("unity-job-7", "7")
        to_stop, safe_delete, deferred = classify_stale_running(
            [job],
            {"7": {"inspection_failed": True}},
        )
        assert to_stop == {}
        assert safe_delete == []
        assert deferred == ["unity-job-7"]
