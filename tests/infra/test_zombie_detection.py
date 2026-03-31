"""
Tests for zombie container detection in POST /infra/job/start.

A zombie is a K8s Job that has been claimed (labeled ``assistant-id=X``,
``unity-status=running``) but whose container never initialized. The
container proves it is alive by writing a ``unity-startup-ack`` label.

The ``start_job`` endpoint must:
1. Trust containers that have the ack label (healthy).
2. Trust containers younger than the grace period (just claimed).
3. Suspend containers past the grace period with no ack (zombies)
   and fall through to claim a new one.
4. Use ``unity-claim-ts`` (not job creation time) to measure the
   grace period, so pool pods with old creation timestamps are not
   incorrectly zombied during initialization.
"""

import time
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
    name: str,
    assistant_id: str,
    *,
    active: int = 1,
    minutes_ago: int = 0,
    ack_ts: str | None = None,
    claim_ts: str | None = None,
    extra_labels: dict | None = None,
):
    """Build a mock K8s Job with optional startup-ack and claim-ts labels."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    job = MagicMock()
    job.metadata.name = name
    job.metadata.labels = {
        "app": "unity",
        "assistant-id": assistant_id,
        "unity-status": "running",
        **({"unity-startup-ack": ack_ts} if ack_ts else {}),
        **({"unity-claim-ts": claim_ts} if claim_ts else {}),
        **(extra_labels or {}),
    }
    job.metadata.resource_version = "99999"
    job.metadata.creation_timestamp = ts
    job.metadata.deletion_timestamp = None
    job.status.active = active or None
    job.status.succeeded = None
    job.status.failed = None
    return job


def _mock_k8s(existing_jobs: list, idle_jobs: list | None = None):
    """Patch _get_k8s_clients so list_namespaced_job returns the given jobs.

    The start_job endpoint makes two list calls:
    1. assistant-id=X (existing check) -> existing_jobs
    2. unity-status=idle (claim_idle_container) -> idle_jobs
    """
    batch_api = MagicMock()
    coord_api = MagicMock()

    existing_result = MagicMock()
    existing_result.items = existing_jobs

    idle_result = MagicMock()
    idle_result.items = idle_jobs or []

    def list_side_effect(namespace, label_selector="", **kwargs):
        if "assistant-id" in label_selector:
            return existing_result
        return idle_result

    batch_api.list_namespaced_job.side_effect = list_side_effect

    return (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), coord_api),
        ),
        batch_api,
        coord_api,
    )


def _start_job_form(assistant_id: str = "82") -> dict:
    """Minimal form data for POST /infra/job/start."""
    return {
        "api_key": "test_key",
        "medium": "unify_message",
        "assistant_id": assistant_id,
        "user_id": "user_123",
        "user_first_name": "Peter",
        "user_surname": "Scholes",
        "user_email": "peter@test.com",
        "assistant_first_name": "Oliver",
        "assistant_surname": "Peterson",
        "assistant_age": "35",
        "assistant_nationality": "UK",
        "assistant_about": "Test assistant",
        "assistant_timezone": "UTC",
    }


class TestZombieDetection:
    """start_job must detect and replace zombie containers."""

    def test_acked_container_trusted(self, client):
        """A container with unity-startup-ack is trusted as healthy."""
        acked_job = _make_k8s_job(
            "unity-healthy-job",
            "82",
            minutes_ago=30,
            ack_ts="1711800000",
        )
        k8s_patch, batch_api, coord_api = _mock_k8s([acked_job])

        with k8s_patch, patch("communication.infra.views.SETTINGS") as mock_settings:
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "Assistant already has a running container"
        assert body["job_name"] == "unity-healthy-job"

        batch_api.patch_namespaced_job.assert_not_called()

    def test_young_container_trusted_without_ack(self, client):
        """A container younger than ACK_GRACE_PERIOD is trusted even without ack."""
        young_job = _make_k8s_job(
            "unity-young-job",
            "82",
            minutes_ago=0,
        )
        k8s_patch, batch_api, coord_api = _mock_k8s([young_job])

        with k8s_patch, patch("communication.infra.views.SETTINGS") as mock_settings:
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "Assistant already has a running container"

        batch_api.patch_namespaced_job.assert_not_called()

    def test_old_container_without_ack_is_zombie(self, client):
        """A container past ACK_GRACE_PERIOD with no ack is suspended."""
        zombie_job = _make_k8s_job(
            "unity-zombie-job",
            "82",
            minutes_ago=10,
            claim_ts=str(int(time.time()) - 600),
        )

        idle_job = MagicMock()
        idle_job.metadata.name = "unity-idle-fresh"
        idle_job.metadata.labels = {
            "app": "unity",
            "unity-status": "idle",
            "unity-image-hash": "abc123",
        }
        idle_job.metadata.resource_version = "11111"
        idle_job.metadata.creation_timestamp = datetime.now(timezone.utc)
        idle_job.status.active = 1

        k8s_patch, batch_api, coord_api = _mock_k8s([zombie_job], [idle_job])

        coord_api.read_namespaced_lease.side_effect = Exception("not found")
        batch_api.patch_namespaced_job.return_value = MagicMock()

        with (
            k8s_patch,
            patch("communication.infra.views.SETTINGS") as mock_settings,
            patch(
                "communication.infra.views.release_assignment_lease",
            ),
        ):
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        suspend_calls = [
            c
            for c in batch_api.patch_namespaced_job.call_args_list
            if c.kwargs.get("body", {}).get("spec", {}).get("suspend") is True
            or (
                len(c.args) >= 1
                and isinstance(c.kwargs.get("body"), dict)
                and c.kwargs["body"].get("spec", {}).get("suspend") is True
            )
        ]
        assert len(suspend_calls) >= 1, (
            f"Expected zombie to be suspended, but patch calls were: "
            f"{batch_api.patch_namespaced_job.call_args_list}"
        )

        suspended_name = suspend_calls[0].kwargs.get(
            "name",
            suspend_calls[0].args[0] if suspend_calls[0].args else None,
        )
        assert suspended_name == "unity-zombie-job"

    def test_completed_job_ignored(self, client):
        """Jobs with active=0 (completed) are not treated as running or zombie."""
        completed_job = _make_k8s_job(
            "unity-completed-job",
            "82",
            active=0,
            minutes_ago=60,
        )

        idle_job = MagicMock()
        idle_job.metadata.name = "unity-idle-fresh"
        idle_job.metadata.labels = {
            "app": "unity",
            "unity-status": "idle",
            "unity-image-hash": "abc123",
        }
        idle_job.metadata.resource_version = "11111"
        idle_job.metadata.creation_timestamp = datetime.now(timezone.utc)
        idle_job.status.active = 1

        k8s_patch, batch_api, coord_api = _mock_k8s([completed_job], [idle_job])

        coord_api.read_namespaced_lease.side_effect = Exception("not found")
        batch_api.patch_namespaced_job.return_value = MagicMock()

        with (
            k8s_patch,
            patch("communication.infra.views.SETTINGS") as mock_settings,
            patch(
                "communication.infra.views.release_assignment_lease",
            ),
        ):
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        assert resp.status_code in (200, 202, 500)
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("message") != "Assistant already has a running container"

    def test_pool_pod_with_recent_claim_ts_trusted(self, client):
        """A pool pod claimed seconds ago (old creation, recent claim-ts) must
        NOT be treated as a zombie, even without an ack yet.

        This is the pre-hire race scenario: the hiring flow calls start_job
        twice in quick succession (wakeup + log_pre_hire_chat). The second
        call arrives before the pod writes its ack, but the claim-ts proves
        the pod was just assigned.
        """
        pool_pod = _make_k8s_job(
            "unity-pool-pod",
            "82",
            minutes_ago=45,
            claim_ts=str(int(time.time()) - 5),
        )
        k8s_patch, batch_api, coord_api = _mock_k8s([pool_pod])

        with k8s_patch, patch("communication.infra.views.SETTINGS") as mock_settings:
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "Assistant already has a running container"
        assert body["job_name"] == "unity-pool-pod"

        batch_api.patch_namespaced_job.assert_not_called()

    def test_pool_pod_with_old_claim_ts_and_no_ack_is_zombie(self, client):
        """A pool pod with an old claim-ts and no ack is a true zombie,
        regardless of how old the job creation time is."""
        zombie_pool_pod = _make_k8s_job(
            "unity-zombie-pool",
            "82",
            minutes_ago=120,
            claim_ts=str(int(time.time()) - 300),
        )

        idle_job = MagicMock()
        idle_job.metadata.name = "unity-idle-replacement"
        idle_job.metadata.labels = {
            "app": "unity",
            "unity-status": "idle",
            "unity-image-hash": "abc123",
        }
        idle_job.metadata.resource_version = "11111"
        idle_job.metadata.creation_timestamp = datetime.now(timezone.utc)
        idle_job.status.active = 1

        k8s_patch, batch_api, coord_api = _mock_k8s(
            [zombie_pool_pod],
            [idle_job],
        )

        coord_api.read_namespaced_lease.side_effect = Exception("not found")
        batch_api.patch_namespaced_job.return_value = MagicMock()

        with (
            k8s_patch,
            patch("communication.infra.views.SETTINGS") as mock_settings,
            patch("communication.infra.views.release_assignment_lease"),
        ):
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        suspend_calls = [
            c
            for c in batch_api.patch_namespaced_job.call_args_list
            if isinstance(c.kwargs.get("body"), dict)
            and c.kwargs["body"].get("spec", {}).get("suspend") is True
        ]
        assert len(suspend_calls) >= 1, (
            f"Expected zombie to be suspended, but patch calls were: "
            f"{batch_api.patch_namespaced_job.call_args_list}"
        )

        suspended_name = suspend_calls[0].kwargs.get(
            "name",
            suspend_calls[0].args[0] if suspend_calls[0].args else None,
        )
        assert suspended_name == "unity-zombie-pool"

    def test_legacy_job_without_claim_ts_falls_back_to_creation(self, client):
        """Jobs without unity-claim-ts (created before this fix was deployed)
        fall back to creation_timestamp for the age check."""
        legacy_zombie = _make_k8s_job(
            "unity-legacy-zombie",
            "82",
            minutes_ago=10,
        )

        idle_job = MagicMock()
        idle_job.metadata.name = "unity-idle-replacement"
        idle_job.metadata.labels = {
            "app": "unity",
            "unity-status": "idle",
            "unity-image-hash": "abc123",
        }
        idle_job.metadata.resource_version = "11111"
        idle_job.metadata.creation_timestamp = datetime.now(timezone.utc)
        idle_job.status.active = 1

        k8s_patch, batch_api, coord_api = _mock_k8s(
            [legacy_zombie],
            [idle_job],
        )

        coord_api.read_namespaced_lease.side_effect = Exception("not found")
        batch_api.patch_namespaced_job.return_value = MagicMock()

        with (
            k8s_patch,
            patch("communication.infra.views.SETTINGS") as mock_settings,
            patch("communication.infra.views.release_assignment_lease"),
        ):
            mock_settings.default_namespace = "production"
            resp = client.post("/infra/job/start", data=_start_job_form())

        suspend_calls = [
            c
            for c in batch_api.patch_namespaced_job.call_args_list
            if isinstance(c.kwargs.get("body"), dict)
            and c.kwargs["body"].get("spec", {}).get("suspend") is True
        ]
        assert (
            len(suspend_calls) >= 1
        ), f"Legacy zombie should still be suspended via creation_timestamp fallback"


class TestClaimIdleContainerClaimTs:
    """claim_idle_container must write a unity-claim-ts label."""

    def test_claim_writes_claim_ts_label(self):
        """The claimed job's labels must include unity-claim-ts."""
        from communication.infra.helpers import claim_idle_container

        idle_job = MagicMock()
        idle_job.metadata.name = "unity-idle-pod"
        idle_job.metadata.resource_version = "12345"
        idle_job.metadata.labels = {
            "app": "unity",
            "unity-status": "idle",
        }
        idle_job.metadata.creation_timestamp = datetime.now(timezone.utc)
        idle_job.status.active = 1

        batch_api = MagicMock()
        list_result = MagicMock()
        list_result.items = [idle_job]
        batch_api.list_namespaced_job.return_value = list_result
        batch_api.patch_namespaced_job.return_value = MagicMock()

        before = int(time.time())
        result = claim_idle_container(
            batch_api,
            "159",
            "production",
            '{"test": true}',
        )
        after = int(time.time())

        assert result == "unity-idle-pod"

        patch_call = batch_api.patch_namespaced_job.call_args
        patched_labels = patch_call.kwargs["body"]["metadata"]["labels"]

        assert "unity-claim-ts" in patched_labels
        claim_ts = int(patched_labels["unity-claim-ts"])
        assert before <= claim_ts <= after
        assert patched_labels["unity-status"] == "running"
        assert patched_labels["assistant-id"] == "159"
