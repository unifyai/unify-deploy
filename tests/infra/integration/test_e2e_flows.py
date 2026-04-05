"""
End-to-end flow tests for the assistant wakeup -> container lifecycle.

These tests exercise the full production code path from the adapter's
/assistant/wakeup endpoint through container creation and session resume.
They hit real deployed services (Adapters, Comms App, GKE) with real
assistant data from Orchestra.

Unlike the infrastructure invariant tests (test_lifecycle, test_concurrency),
these tests focus on user-facing behavioral contracts: response time,
container creation, and session continuity after restart.
"""

import json
import time

import pytest
import requests

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    cleanup_assistant_jobs,
    expire_test_assistant_records,
    list_assigned_vms,
    poll_until,
    pull_outbound_messages,
    replenish_pool,
    send_test_message,
    wait_for_assistant_runtime_stopped,
    wait_for_assistant_container_ready,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration]


def _wakeup(assistant_id: str):
    """POST /assistant/wakeup and return (response, elapsed_seconds)."""
    t0 = time.monotonic()
    resp = requests.post(
        f"{ADAPTERS_URL}/assistant/wakeup",
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return resp, time.monotonic() - t0


class TestE2EFlows:
    """End-to-end flows exercising the adapter -> comms app -> K8s pipeline."""

    @pytest.mark.timeout(60)
    def test_wakeup_responds_within_orchestra_timeout(
        self,
        real_assistant_data,
        batch_api,
        comms,
    ):
        """Wakeup must return within Orchestra's httpx timeout (20s).

        Orchestra calls POST /assistant/wakeup with a ~20s httpx timeout.
        If wakeup blocks longer (e.g. ThreadPoolExecutor contention with
        blocking K8s API calls sharing the default executor), Orchestra gets
        a ReadTimeout and the user sees a 500 on the console "Go Online"
        button.

        Asserts < 15s to leave a 5s safety margin for network variance.
        """
        assistant_id = str(real_assistant_data["assistant_id"])

        try:
            resp, elapsed = _wakeup(assistant_id)

            assert (
                resp.status_code == 200
            ), f"Wakeup failed: {resp.status_code} {resp.text}"
            assert elapsed < 15.0, (
                f"Wakeup took {elapsed:.1f}s, exceeding the 15s budget "
                f"(Orchestra's ~20s httpx timeout minus 5s margin). "
                f"This causes a 500 error on the console 'Go Online' button."
            )
            print(f"\n[Wakeup] Responded in {elapsed:.1f}s (budget: 15s)")
        finally:
            try:
                cleanup_assistant_jobs(batch_api, [assistant_id])
            except Exception:
                pass
            replenish_pool()

    @pytest.mark.timeout(300)
    def test_wakeup_starts_container_for_real_assistant(
        self,
        real_assistant_data,
        batch_api,
        comms,
    ):
        """Wakeup must create a K8s Job with correct labels and startup config.

        Exercises the full path: adapter receives POST /assistant/wakeup ->
        comms app claims an idle container -> K8s Job is labeled with the
        assistant's ID and annotated with the startup configuration.

        Verifies the contract between Orchestra, the adapter, and the comms
        app that the container is correctly provisioned for the preview
        startup protocol (AssistantSession binding on the Job).
        """
        assistant_id = str(real_assistant_data["assistant_id"])

        try:
            expire_test_assistant_records(assistant_id)
            cleanup_assistant_jobs(batch_api, [assistant_id])
            time.sleep(5)

            resp, elapsed = _wakeup(assistant_id)

            assert (
                resp.status_code == 200
            ), f"Wakeup failed: {resp.status_code} {resp.text}"
            assert (
                elapsed < 15.0
            ), f"Wakeup took {elapsed:.1f}s, exceeding the 15s timeout budget"

            jobs = wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )

            assert len(jobs) == 1, (
                f"Expected exactly 1 Job for assistant {assistant_id}, "
                f"got {len(jobs)}: {[j.metadata.name for j in jobs]}"
            )

            job = jobs[0]
            labels = dict(job.metadata.labels or {})
            annotations = dict(job.metadata.annotations or {})
            sanitized = assistant_id.lower().replace("_", "-")

            assert (
                labels.get("unity-status") == "running"
            ), f"Expected unity-status=running, got {labels.get('unity-status')}"
            assert labels.get("assistant-id") == sanitized, (
                f"Expected assistant-id={sanitized}, "
                f"got {labels.get('assistant-id')}"
            )
            assert "assistantsession.unify.ai/name" in annotations, (
                f"Expected assistantsession annotation, "
                f"got keys: {list(annotations.keys())}"
            )

            print(
                f"\n[Wakeup->Container] Job {job.metadata.name} created in "
                f"{elapsed:.1f}s with correct labels and AssistantSession binding",
            )
        finally:
            cleanup_assistant_jobs(batch_api, [assistant_id])
            replenish_pool()

    @pytest.mark.timeout(300)
    def test_session_resume_after_container_stop(
        self,
        real_assistant_data,
        batch_api,
        comms,
    ):
        """After stopping a runtime, a second wakeup must start a fresh one.

        Exercises the full session lifecycle: wakeup -> container runs -> stop
        -> wakeup again -> new container runs. The second container must be a
        different K8s Job (not the old one restarting) with correct labels.

        Guards against regressions where stopped containers leave stale
        AssistantJobs records or K8s resources that block re-assignment of
        the assistant to a new container.
        """
        assistant_id = str(real_assistant_data["assistant_id"])

        try:
            expire_test_assistant_records(assistant_id)
            cleanup_assistant_jobs(batch_api, [assistant_id])
            time.sleep(5)

            resp1, _ = _wakeup(assistant_id)
            assert (
                resp1.status_code == 200
            ), f"First wakeup failed: {resp1.status_code} {resp1.text}"

            jobs = wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            assert jobs, "No container appeared after first wakeup"
            first_job_name = jobs[0].metadata.name
            print(f"\n[Resume] First container: {first_job_name}")

            stop_resp = comms.post(f"/infra/session/{assistant_id}/stop")
            assert (
                stop_resp.status_code == 200
            ), f"Stop failed: {stop_resp.status_code} {stop_resp.text}"

            wait_for_assistant_runtime_stopped(
                assistant_id,
                batch_api=batch_api,
                timeout=240,
            )
            expire_test_assistant_records(assistant_id)
            print("[Resume] First runtime stopped")

            replenish_pool()
            time.sleep(10)

            resp2, elapsed2 = _wakeup(assistant_id)
            assert (
                resp2.status_code == 200
            ), f"Second wakeup failed: {resp2.status_code} {resp2.text}"
            assert elapsed2 < 15.0, (
                f"Second wakeup took {elapsed2:.1f}s, exceeding the 15s "
                f"timeout budget"
            )

            jobs2 = wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            assert jobs2, "No container appeared after second wakeup"
            second_job_name = jobs2[0].metadata.name

            assert second_job_name != first_job_name, (
                f"Second container has the same Job name ({second_job_name}) "
                f"as the first. The old Job may have restarted instead of "
                f"being replaced by a fresh container."
            )

            labels = dict(jobs2[0].metadata.labels or {})
            sanitized = assistant_id.lower().replace("_", "-")
            assert labels.get("unity-status") == "running", (
                f"Expected unity-status=running on second container, "
                f"got {labels.get('unity-status')}"
            )
            assert labels.get("assistant-id") == sanitized, (
                f"Expected assistant-id={sanitized} on second container, "
                f"got {labels.get('assistant-id')}"
            )

            print(
                f"[Resume] Second container: {second_job_name} "
                f"(different from first: {first_job_name})",
            )
        finally:
            cleanup_assistant_jobs(batch_api, [assistant_id])
            replenish_pool()

    @pytest.mark.timeout(300)
    def test_wakeup_then_message_reaches_pubsub(
        self,
        real_assistant_data,
        batch_api,
        core_api,
        gce_client,
        comms,
    ):
        """After wakeup, a message sent via /unify/message must produce a reply.

        Exercises the full round-trip: adapter publishes message to Pub/Sub ->
        Unity container receives it -> LLM processes -> reply published to
        outbound subscription.

        Uses a clean slate (expire + cleanup) to avoid interference from
        containers created by earlier tests. Sends a deterministic prompt
        that instructs the assistant to reply with a unique token, avoiding
        flakiness from the assistant deciding to stay silent.
        """
        from google.cloud import pubsub_v1 as _pubsub_v1

        assistant_id = str(real_assistant_data["assistant_id"])
        subscriber = _pubsub_v1.SubscriberClient()

        try:
            expire_test_assistant_records(assistant_id)
            cleanup_assistant_jobs(batch_api, [assistant_id])
            time.sleep(10)

            resp, elapsed = _wakeup(assistant_id)
            assert (
                resp.status_code == 200
            ), f"Wakeup failed: {resp.status_code} {resp.text}"

            jobs = wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            job_name = jobs[0].metadata.name

            print(
                f"[Message] Container {job_name} running, waiting for ContainerReady...",
            )
            ready_session = wait_for_assistant_container_ready(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                timeout=300,
                interval=5,
            )
            ready_phase = (ready_session.get("status") or {}).get("phase") or ""
            print(
                f"[Message] AssistantSession ContainerReady reached "
                f"(phase={ready_phase or 'unknown'}), waiting 15s for manager init...",
            )
            time.sleep(15)

            token = f"E2E_ACK_{int(time.time())}"
            msg_body = (
                "Please reply with exactly this token and nothing else: "
                f"{token}. This is a deterministic integration test."
            )
            msg_resp = send_test_message(real_assistant_data, body=msg_body)
            assert (
                msg_resp.status_code == 200
            ), f"Message send failed: {msg_resp.status_code} {msg_resp.text}"
            print(
                f"[Message] Sent token prompt '{token}', polling for outbound reply...",
            )

            def _messages_with_token():
                msgs = pull_outbound_messages(subscriber, assistant_id, timeout=10)
                return [m for m in msgs if token in json.dumps(m, sort_keys=True)]

            messages = poll_until(
                _messages_with_token,
                timeout=120,
                interval=10,
                description=f"Outbound reply containing {token} for assistant {assistant_id}",
            )

            assert (
                messages
            ), f"No outbound reply containing {token} received within 120s"
            print(
                f"[Message] Received {len(messages)} outbound message(s) containing {token}",
            )
        finally:
            cleanup_assistant_jobs(batch_api, [assistant_id])
            replenish_pool()

    @pytest.mark.timeout(300)
    def test_wakeup_assigns_vm_for_desktop_assistant(
        self,
        real_assistant_data,
        batch_api,
        gce_client,
        comms,
    ):
        """After wakeup, a desktop-mode assistant must get a VM assigned.

        Exercises the full chain: wakeup -> container claim -> VM assignment.
        Verifies a GCE VM appears with pool-role=assigned and the correct
        assistant-id label. Tests B and C verify the container but not the
        VM; this test closes that gap.
        """
        assistant_id = str(real_assistant_data["assistant_id"])
        desktop_mode = real_assistant_data.get("desktop_mode", "")
        if desktop_mode not in ("ubuntu", "windows"):
            pytest.skip(
                f"Assistant {assistant_id} has desktop_mode='{desktop_mode}', "
                f"skipping VM assignment test",
            )

        if gce_client is None:
            pytest.skip("GCE client not available")

        try:
            expire_test_assistant_records(assistant_id)
            cleanup_assistant_jobs(batch_api, [assistant_id])
            time.sleep(5)

            resp, elapsed = _wakeup(assistant_id)
            assert (
                resp.status_code == 200
            ), f"Wakeup failed: {resp.status_code} {resp.text}"

            wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )

            vms = poll_until(
                lambda: list_assigned_vms(gce_client, assistant_id),
                timeout=180,
                interval=15,
                description=f"VM assignment for assistant {assistant_id}",
            )

            assert (
                len(vms) >= 1
            ), f"No VM assigned to assistant {assistant_id} within 180s"
            vm = vms[0]
            labels = dict(vm.labels or {})
            sanitized = assistant_id.lower().replace("_", "-")
            assert (
                labels.get("pool-role") == "assigned"
            ), f"Expected pool-role=assigned, got {labels.get('pool-role')}"
            assert labels.get("assistant-id") == sanitized, (
                f"Expected assistant-id={sanitized}, "
                f"got {labels.get('assistant-id')}"
            )
            print(
                f"\n[VM] {vm.name} assigned to assistant {assistant_id} "
                f"(pool-role=assigned)",
            )
        finally:
            cleanup_assistant_jobs(batch_api, [assistant_id])
            replenish_pool()
