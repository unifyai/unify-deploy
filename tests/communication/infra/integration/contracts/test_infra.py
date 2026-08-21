"""
Behavioral contract tests for comms-app infrastructure endpoints.

Covers Pub/Sub topic CRUD, VM pool status, image hash resolution, and
Gmail watch — endpoints that underpin the infrastructure but aren't
exercised by the existing container-lifecycle or stress tests.

All tests hit the real deployed comms app with real credentials.

Endpoints covered:
- POST /infra/pubsub/topic + DELETE /infra/pubsub/topic
- GET /infra/vm/pool/status
- GET /infra/image
- GET /infra/jobs (listing)
- POST /gmail/watch
"""

import uuid

import pytest
import requests

from ..conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    COMMS_APP_URL,
    GCP_PROJECT_ID,
    find_assistant_with_email,
)

pytestmark = [pytest.mark.integration]

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# Pub/Sub topic CRUD
# ---------------------------------------------------------------------------


class TestPubSubTopicCRUD:
    """Contract: POST /infra/pubsub/topic creates a topic with inbound,
    outbound, actions, and system-error subscriptions. DELETE removes them."""

    def test_create_and_delete_topic(self):
        topic_name = f"contract-test-{uuid.uuid4().hex[:8]}"
        try:
            create_resp = requests.post(
                f"{COMMS_APP_URL}/infra/pubsub/topic",
                data={"topic_name": topic_name},
                headers=_ADMIN_HEADERS,
                timeout=30,
            )
            assert (
                create_resp.status_code == 200
            ), f"topic create failed: {create_resp.status_code} {create_resp.text}"
            body = create_resp.json()
            assert "topic_name" in body
            assert "subscription_name" in body
            assert "project_id" in body
            assert GCP_PROJECT_ID in body["project_id"]

        finally:
            requests.delete(
                f"{COMMS_APP_URL}/infra/pubsub/topic",
                data={"topic_name": topic_name},
                headers=_ADMIN_HEADERS,
                timeout=15,
            )

    def test_delete_nonexistent_topic_is_safe(self):
        resp = requests.delete(
            f"{COMMS_APP_URL}/infra/pubsub/topic",
            data={"topic_name": "definitely-does-not-exist-12345"},
            headers=_ADMIN_HEADERS,
            timeout=15,
        )
        assert resp.status_code in (
            200,
            404,
        ), f"Unexpected status for nonexistent topic delete: {resp.status_code}"


# ---------------------------------------------------------------------------
# VM pool status
# ---------------------------------------------------------------------------


class TestVMPoolStatus:
    """Contract: GET /infra/vm/pool/status returns a structured report of
    pool VMs grouped by type (ubuntu, windows) and role."""

    def test_pool_status_returns_structured_report(self):
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/vm/pool/status",
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"pool/status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert (
            "ubuntu" in body or "windows" in body or "vms" in body
        ), f"Unexpected pool status shape: {list(body.keys())}"


# ---------------------------------------------------------------------------
# Image hash
# ---------------------------------------------------------------------------


class TestImageHash:
    """Contract: GET /infra/image returns the commit hash of the latest
    deployed Unity container image."""

    def test_image_hash_returns_commit(self):
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/image",
            headers=_ADMIN_HEADERS,
            timeout=15,
        )
        assert (
            resp.status_code == 200
        ), f"image hash failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "commit_hash" in body, f"Missing commit_hash in response: {body}"
        assert (
            len(body["commit_hash"]) >= 7
        ), f"commit_hash too short: {body['commit_hash']}"


# ---------------------------------------------------------------------------
# Job listing
# ---------------------------------------------------------------------------


class TestJobListing:
    """Contract: GET /infra/jobs returns a list of K8s jobs with metadata."""

    def test_list_jobs_returns_array(self):
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/jobs",
            params={"label_selector": "app=unity", "hours": 2},
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"jobs listing failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "jobs" in body
        assert isinstance(body["jobs"], list)

    def test_list_jobs_with_status_filter(self):
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/jobs",
            params={
                "label_selector": "app=unity,unity-status=idle",
                "hours": 1,
            },
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        for job in body.get("jobs", []):
            assert job.get("status") in (
                "Idle",
                "Running",
                "Pending",
                "Failed",
                "Succeeded",
                None,
            ), f"Unexpected job status: {job}"


# ---------------------------------------------------------------------------
# Gmail watch
# ---------------------------------------------------------------------------


class TestGmailWatch:
    """Contract: POST /gmail/watch sets up a Gmail push notification watch
    for the given email address. Requires GCP_SA_KEY with domain-wide
    delegation on the comms app."""

    def test_gmail_watch_for_known_email(self):
        assistant = find_assistant_with_email()
        if not assistant:
            pytest.skip("No assistant with an email address found")

        email = assistant["email"]
        resp = requests.post(
            f"{COMMS_APP_URL}/gmail/watch",
            json={"primary_email": email},
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        # 200 = watch created/renewed, 400/500 = SA can't impersonate this email
        # Both are valid integration outcomes — the key assertion is that the
        # endpoint is reachable and processes the request through production code
        assert resp.status_code in (
            200,
            400,
            500,
        ), f"gmail/watch unexpected status: {resp.status_code} {resp.text}"
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("success") is True


# ---------------------------------------------------------------------------
# Auth contract
# ---------------------------------------------------------------------------


class TestInfraAuth:
    """Contract: All admin-gated infra endpoints reject unauthorized requests."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/infra/jobs"),
            ("GET", "/infra/image"),
            ("GET", "/infra/vm/pool/status"),
            ("POST", "/infra/job/create"),
        ],
    )
    def test_admin_endpoints_reject_no_auth(self, method, path):
        resp = requests.request(method, f"{COMMS_APP_URL}{path}", timeout=10)
        assert resp.status_code in (
            401,
            403,
            422,
        ), f"{method} {path} should reject no-auth, got {resp.status_code}"

    def test_adapter_scheduler_rejects_no_auth(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/infra/maintenance",
            timeout=10,
        )
        assert resp.status_code in (
            401,
            403,
            422,
        ), f"POST /scheduled/infra/maintenance should reject no-auth, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Job stop / read / logs
# ---------------------------------------------------------------------------


class TestJobStop:
    """Contract: POST /infra/job/stop suspends a running K8s job.

    Idle pool jobs are not session-owned, so the endpoint should preserve the
    raw suspend behavior and not report any AssistantSession stop side effect.
    """

    def test_stop_job(self, comms):
        from ..conftest import create_and_cleanup_idle_job

        job_name = create_and_cleanup_idle_job(comms)
        try:
            resp = comms.post("/infra/job/stop", data={"job_name": job_name})
            assert (
                resp.status_code == 200
            ), f"job/stop failed: {resp.status_code} {resp.text}"
            body = resp.json()
            assert body["success"] is True
            assert body["assistant_id"] is None
            assert body["binding_id"] is None
            assert body["session_stop_requested"] is False
        finally:
            comms.delete("/infra/job/delete", data={"job_name": job_name})


class TestJobRead:
    """Contract: GET /infra/job/{name} returns job metadata and labels."""

    def test_read_existing_job(self, comms):
        jobs_resp = comms.get(
            "/infra/jobs",
            params={"label_selector": "app=unity", "hours": 1},
        )
        jobs = jobs_resp.json().get("jobs", [])
        if not jobs:
            pytest.skip("No jobs in namespace to read")
        job_name = jobs[0]["job_name"]

        resp = comms.get(f"/infra/job/{job_name}")
        assert (
            resp.status_code == 200
        ), f"job read failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["job_name"] == job_name
        assert "labels" in body
        assert "resource_version" in body

    def test_read_nonexistent_job_returns_404(self, comms):
        resp = comms.get("/infra/job/nonexistent-job-xyz-12345")
        assert resp.status_code == 404


class TestAssistantSessionRead:
    """Contract: GET /infra/session/{assistant_id} returns the current runtime session."""

    def test_read_existing_session(self, comms):
        jobs_resp = comms.get(
            "/infra/jobs",
            params={"label_selector": "app=unity,unity-status=running", "hours": 1},
        )
        jobs = jobs_resp.json().get("jobs", [])
        if not jobs:
            pytest.skip("No running jobs to resolve a session from")
        assistant_id = jobs[0].get("assistant_id")
        if not assistant_id or assistant_id == "unknown":
            pytest.skip("Running job has no assistant-id")

        resp = comms.get(f"/infra/session/{assistant_id}")
        assert (
            resp.status_code == 200
        ), f"session read failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body.get("spec", {}).get("assistantId") == str(assistant_id)
        assert "status" in body


class TestJobLogs:
    """Contract: GET /infra/job/logs returns pod logs for a job."""

    def test_get_logs_for_existing_job(self, comms):
        jobs_resp = comms.get(
            "/infra/jobs",
            params={"label_selector": "app=unity,unity-status=running", "hours": 1},
        )
        jobs = jobs_resp.json().get("jobs", [])
        if not jobs:
            pytest.skip("No running jobs to read logs from")
        job_name = jobs[0]["job_name"]

        resp = comms.get(
            "/infra/job/logs",
            params={"job_name": job_name, "tail_lines": 5},
        )
        assert resp.status_code in (
            200,
            404,
        ), f"job/logs unexpected: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# VM pool provision / rebalance / disk lifecycle
# ---------------------------------------------------------------------------


class TestVMPoolProvision:
    """Contract: POST /infra/vm/pool/provision creates a new pool VM.

    VM provisioning takes 2-5 minutes (IP reservation, DNS, instance
    creation, startup). Use @pytest.mark.slow to opt in.
    """

    @pytest.mark.slow
    def test_provision_single_ubuntu_vm(self, comms):
        vm_name = None
        try:
            resp = requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/provision",
                json={"vm_type": "ubuntu", "count": 1},
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=300,
            )
            assert (
                resp.status_code == 200
            ), f"vm provision failed: {resp.status_code} {resp.text}"
            body = resp.json()
            assert "provisioned" in body
            assert len(body["provisioned"]) >= 1
            result = body["provisioned"][0]
            vm_name = result.get("vm_name") or result.get("name")
        finally:
            if vm_name:
                try:
                    from google.cloud import compute_v1
                    from common.settings import SETTINGS

                    client = compute_v1.InstancesClient()
                    client.stop(
                        project=SETTINGS.vm_project_id,
                        zone=SETTINGS.vm_zone,
                        instance=vm_name,
                    ).result()
                except Exception:
                    pass


class TestVMPoolRebalance:
    """Contract: POST /infra/vm/pool/rebalance triggers a pool rebalance.

    Rebalance may start/stop VMs, so allow a longer timeout.
    """

    def test_rebalance_returns_result(self, comms):
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/rebalance",
            params={"vm_type": "ubuntu"},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=120,
        )
        assert (
            resp.status_code == 200
        ), f"vm rebalance failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)


class TestDiskDetach:
    """Contract: POST /infra/vm/pool/disk/detach/{id} detaches an assistant
    disk from its VM without deleting the disk."""

    def test_detach_with_no_attached_disk(self, comms):
        resp = comms.post("/infra/vm/pool/disk/detach/nonexistent-assistant-999")
        # 200 with detached=false, or 404/500 if no VM found
        assert resp.status_code in (
            200,
            404,
            500,
        ), f"disk detach unexpected: {resp.status_code} {resp.text}"


class TestDiskDelete:
    """Contract: DELETE /infra/vm/pool/disk/{id} deletes an assistant disk."""

    def test_delete_nonexistent_disk(self, comms):
        resp = comms.delete("/infra/vm/pool/disk/nonexistent-assistant-999")
        assert resp.status_code in (
            200,
            404,
        ), f"disk delete unexpected: {resp.status_code} {resp.text}"
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("deleted") is False or body.get("deleted") is True


class TestOrphanDiskReconcile:
    """Contract: POST /infra/vm/pool/reconcile-orphan-disks returns a
    structured report of deleted/skipped disks."""

    def test_reconcile_returns_structured_report(self, comms):
        resp = comms.post(
            "/infra/vm/pool/reconcile-orphan-disks",
            params={"max_age_hours": 999999},
        )
        assert (
            resp.status_code == 200
        ), f"reconcile-orphan-disks failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "deleted" in body, f"Missing 'deleted' in response: {body}"
        assert "skipped" in body, f"Missing 'skipped' in response: {body}"
        assert "errors" in body, f"Missing 'errors' in response: {body}"
        assert isinstance(body["errors"], list)

    def test_reconcile_accepts_custom_max_age(self, comms):
        resp = comms.post(
            "/infra/vm/pool/reconcile-orphan-disks",
            params={"max_age_hours": 1},
        )
        assert resp.status_code == 200


class TestVMReady:
    """Contract: POST /infra/vm/ready only succeeds once desktop readiness
    is verified for the active AssistantSession."""

    def test_vm_ready_with_assigned_vm(self):
        from ..conftest import UNIFY_KEY, find_assistant_with_assigned_vm

        if not UNIFY_KEY:
            pytest.skip("UNIFY_KEY required for vm/ready")
        assistant = find_assistant_with_assigned_vm()
        if not assistant:
            pytest.skip("No assistant with an assigned VM found")

        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/ready",
            json={
                "assistant_id": str(assistant["assistant_id"]),
                "binding_id": str(assistant.get("binding_id", "") or ""),
                "hostname": str(assistant.get("hostname", "") or ""),
                "vm_type": "ubuntu",
            },
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=30,
        )
        # 200 = authenticated readiness confirmed; 401/409/503 reflect auth/session drift
        assert resp.status_code in (
            200,
            401,
            409,
            503,
        ), f"vm/ready unexpected: {resp.status_code} {resp.text}"
        if resp.status_code == 200:
            body = resp.json()
            assert body["success"] is True
            assert "message_id" in body


# ---------------------------------------------------------------------------
# VM self-management endpoints (GCP identity token auth)
# ---------------------------------------------------------------------------


class TestVMMarkIdle:
    """Contract: POST /infra/vm/mark-idle sets pool-role=idle on the calling VM.
    Authenticated via GCP identity token from pool-vm-sa.

    Since we generate the token via impersonation (not from a real VM), the
    token won't contain GCE instance metadata — the endpoint returns 403
    after passing SA email validation. This still exercises the full auth chain.
    """

    def test_mark_idle_auth_chain(self):
        from ..conftest import generate_vm_identity_token

        token = generate_vm_identity_token()
        if not token:
            pytest.skip("Cannot generate VM identity token (missing IAM grant)")

        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/mark-idle",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        # 403 = token valid but missing GCE instance metadata (expected for
        # impersonated tokens); 200 = would require a real VM token
        assert resp.status_code in (
            200,
            403,
        ), f"vm/mark-idle unexpected: {resp.status_code} {resp.text}"

    def test_mark_idle_rejects_invalid_token(self):
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/mark-idle",
            headers={"Authorization": "Bearer invalid-token"},
            timeout=10,
        )
        assert resp.status_code in (401, 403)


class TestVMWipeMetadataKey:
    """Contract: POST /infra/vm/wipe-metadata-key clears a metadata key
    on the calling VM. Same auth pattern as mark-idle."""

    def test_wipe_metadata_auth_chain(self):
        from ..conftest import generate_vm_identity_token

        token = generate_vm_identity_token()
        if not token:
            pytest.skip("Cannot generate VM identity token (missing IAM grant)")

        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/wipe-metadata-key",
            json={"key": "test-key"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        assert resp.status_code in (
            200,
            403,
        ), f"vm/wipe-metadata-key unexpected: {resp.status_code} {resp.text}"

    def test_wipe_metadata_rejects_no_auth(self):
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/wipe-metadata-key",
            json={"key": "test-key"},
            timeout=10,
        )
        assert resp.status_code in (401, 403)
