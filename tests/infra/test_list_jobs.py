"""
Contract tests for GET /infra/jobs.

These tests codify the response schema that Orchestra depends on for
determining assistant online/offline status. The endpoint queries K8s
for jobs matching a label selector and returns structured status info.

Orchestra expects:
  - jobs[].job_name (str)
  - jobs[].assistant_id (str)
  - jobs[].status ("Running" | "Completed" | "Failed" | "Unknown")
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def _make_k8s_job(
    assistant_id: str,
    active: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    minutes_ago: int = 0,
    labels: dict | None = None,
):
    """Build a mock K8s Job object matching the kubernetes client schema.

    The job name encodes a timestamp (used by the endpoint's time filter),
    so we derive it from the current time minus `minutes_ago`.
    """
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    name = f"unity-{assistant_id}-{ts.strftime('%Y-%m-%d-%H-%M-%S')}"
    job = MagicMock()
    job.metadata.name = name
    job.metadata.labels = {
        "app": "unity",
        "assistant-id": assistant_id,
        "unity-date": ts.strftime("%Y-%m-%d"),
        **(labels or {}),
    }
    job.metadata.resource_version = "12345"
    job.metadata.creation_timestamp = ts
    job.status.active = active or None
    job.status.succeeded = succeeded or None
    job.status.failed = failed or None
    return job


def _mock_k8s_returning(jobs: list):
    """Patch _get_k8s_clients to return a batch_api that lists the given jobs."""
    batch_api = MagicMock()
    result = MagicMock()
    result.items = jobs
    batch_api.list_namespaced_job.return_value = result

    core_api = MagicMock()
    networking_api = MagicMock()

    return patch(
        "communication.infra.views._get_k8s_clients",
        new_callable=AsyncMock,
        return_value=(batch_api, core_api, networking_api),
    )


class TestListJobsContract:

    def test_running_job_reports_running_status(self, client):
        job = _make_k8s_job("abc", active=1)

        with _mock_k8s_returning([job]):
            resp = client.get(
                "/infra/jobs",
                params={"label_selector": "app=unity,assistant-id=abc"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["jobs"]) == 1

        j = data["jobs"][0]
        assert j["status"] == "Running"
        assert j["job_name"].startswith("unity-abc-")
        assert j["assistant_id"] == "abc"

    def test_completed_job_reports_completed_status(self, client):
        job = _make_k8s_job("abc", succeeded=1)

        with _mock_k8s_returning([job]):
            resp = client.get(
                "/infra/jobs",
                params={"label_selector": "app=unity,assistant-id=abc"},
            )

        assert resp.status_code == 200
        j = resp.json()["jobs"][0]
        assert j["status"] == "Completed"

    def test_failed_job_reports_failed_status(self, client):
        job = _make_k8s_job("abc", failed=1)

        with _mock_k8s_returning([job]):
            resp = client.get(
                "/infra/jobs",
                params={"label_selector": "app=unity,assistant-id=abc"},
            )

        assert resp.status_code == 200
        j = resp.json()["jobs"][0]
        assert j["status"] == "Failed"

    def test_empty_result_when_no_jobs(self, client):
        with _mock_k8s_returning([]):
            resp = client.get(
                "/infra/jobs",
                params={"label_selector": "app=unity,assistant-id=nonexistent"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs"] == []
        assert data["total_jobs"] == 0

    def test_mixed_statuses_all_reported(self, client):
        running = _make_k8s_job("abc", active=1, minutes_ago=0)
        completed = _make_k8s_job("abc", succeeded=1, minutes_ago=60)
        failed = _make_k8s_job("abc", failed=1, minutes_ago=120)

        with _mock_k8s_returning([running, completed, failed]):
            resp = client.get(
                "/infra/jobs",
                params={"label_selector": "app=unity,assistant-id=abc"},
            )

        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        statuses = {j["status"] for j in jobs}
        assert statuses == {"Running", "Completed", "Failed"}

    def test_response_schema_has_required_fields(self, client):
        """Verify every job object has the fields Orchestra depends on."""
        job = _make_k8s_job("abc", active=1)

        with _mock_k8s_returning([job]):
            resp = client.get("/infra/jobs")

        j = resp.json()["jobs"][0]
        assert "job_name" in j
        assert "assistant_id" in j
        assert "status" in j
        assert isinstance(j["job_name"], str)
        assert isinstance(j["assistant_id"], str)
        assert j["status"] in ("Running", "Completed", "Failed", "Unknown")
