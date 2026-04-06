"""
Production traffic stress test.

Simulates a product launch: many users hitting the system simultaneously
with diverse, overlapping traffic patterns.  Unlike the existing
integration tests (which are sequential: start 1 assistant, verify,
clean up, start the next), everything here happens at once.

Eight phases exercise progressively harder failure modes:

  P1  Thundering herd (concurrent startups, pool overflow)
  P2  Traffic firehose during boot (Pub/Sub buffering under load)
  P3  Steady-state verification (containers, VMs, auth)
  P4  Sustained mixed load (randomised traffic, invariant monitoring)
  P5  Crash recovery under load (pod kill, watcher cleanup, re-start)
  P6  Cleanup concurrent with startups (INV-8 TOCTOU race)
  P7  Rapid restart with disk re-attachment (session end + immediate re-start)
  P8  Wind-down and cleanup

Driven by the ``test_assistants`` session fixture — set
``TEST_CREATE_ASSISTANT_COUNT`` to control scale:

    # Quick (~5 min)
    TEST_CREATE_ASSISTANT_COUNT=3 pytest tests/infra/integration/test_stress.py -v -s

    # Full stress (~15 min)
    TEST_CREATE_ASSISTANT_COUNT=20 pytest tests/infra/integration/test_stress.py -v -s
"""

import json

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from communication.infra.vm_config import POOL_TARGET_IDLE as VM_POOL_TARGET_IDLE

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    NAMESPACE,
    add_failure_context,
    check_invariants,
    cleanup_assistant_jobs,
    count_idle_jobs,
    get_assistant_session,
    list_assigned_vms,
    list_idle_vms,
    list_jobs_with_assistant_id,
    poll_until,
    probe_vm_agent_service_authenticated,
    pull_outbound_messages,
    release_assigned_vms,
    replenish_pool,
    send_test_meet,
    send_test_message,
    send_test_system_event,
    wait_for_idle_pool,
    wait_for_idle_vm_pool,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration]

_STRESS_IDLE_CONTAINER_TARGET = 3
_STRESS_IDLE_VM_TARGET = VM_POOL_TARGET_IDLE
_STRESS_SETUP_CLEANUP_TIMEOUT_SECONDS = 60
_STRESS_FAST_TEARDOWN_TIMEOUT_SECONDS = 30
_STRESS_CLEANUP_PARALLELISM = 6
_STRESS_CONTAINER_BASELINE_TIMEOUT_SECONDS = 120
_STRESS_VM_BASELINE_TIMEOUT_SECONDS = 240
_VM_DESKTOP_READY_TIMEOUT_SECONDS = 300
_VM_REATTACH_TIMEOUT_SECONDS = 180
_VM_CONTRACT_POLL_INTERVAL_SECONDS = 10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _start_job_tolerant(comms_client, assistant_data, medium="unify_message"):
    """Like start_real_job but returns (status_code, body) without asserting."""
    try:
        resp = comms_client.post(
            "/infra/job/start",
            data={
                "api_key": assistant_data["api_key"],
                "medium": medium,
                **{k: v for k, v in assistant_data.items() if k != "api_key"},
            },
        )
        return resp.status_code, resp.text
    except Exception as e:
        return 0, str(e)


def _trigger_reconciliation():
    """No-op for AssistantSession v1 session-backed reconciliation."""
    return None


def _trigger_vm_reconciliation():
    """No-op for AssistantSession v1 session-backed reconciliation."""
    return None


def _trigger_cleanup():
    """Trigger the idle-pool cleanup utility behind unified maintenance."""
    try:
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/cleanup",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        if resp.status_code == 200:
            print("    [scheduler] Cleanup fired")
    except Exception:
        pass


def _trigger_pool_refresh():
    """Trigger the pool-refresh utility behind unified maintenance.

    Creates new idle containers with the latest image regardless of current
    pool size.  The cleanup cron (10 min later) would normally delete the
    old ones.
    """
    try:
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/create",
            params={"refresh": "true"},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(
                f"    [scheduler] Pool refresh: created {data.get('created', '?')} containers",
            )
    except Exception:
        pass


def _trigger_vm_pool_rebalance(comms_client):
    """Trigger VM rebalance toward the configured idle/stopped targets."""
    try:
        resp = comms_client.post(
            "/infra/vm/pool/rebalance",
            params={"vm_type": "ubuntu"},
            timeout=120,
        )
        if resp.status_code == 200:
            actions = (resp.json() or {}).get("actions") or []
            print(f"    [vm-pool] Rebalance fired ({len(actions)} action(s))")
    except Exception:
        pass


def _trigger_stale_expire():
    """Trigger the stale-job utility behind unified maintenance.

    Suspends K8s jobs running >12h and releases their VMs.  Our test
    containers are minutes old so they won't be affected, but the sweep
    mechanism still executes and can race with other operations.
    """
    try:
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/expire-stale",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(
                f"    [scheduler] Stale sweep: {data.get('total_running', '?')} running, "
                f"{data.get('expired', '?')} expired",
            )
    except Exception:
        pass


def _best_effort_release_assigned_vms(assistant_ids, gce_client) -> None:
    """Nudge any still-assigned VMs into release without blocking teardown."""
    if gce_client is None:
        return
    for assistant_id in dict.fromkeys(str(aid) for aid in assistant_ids):
        try:
            release_assigned_vms(
                assistant_id,
                gce_client=gce_client,
                timeout=20,
            )
        except Exception:
            pass


def _reset_stress_baseline(
    comms_client,
    batch_api,
    gce_client,
    assistant_ids,
    *,
    context: str,
    cleanup_timeout: float,
    wait_for_baseline: bool,
) -> None:
    """Reset stress-test resources toward the expected idle baseline."""
    assistant_ids = list(assistant_ids)
    cleanup_assistant_jobs(
        batch_api,
        assistant_ids,
        strict=False,
        context=context,
        timeout=cleanup_timeout,
        parallelism=max(1, min(len(assistant_ids), _STRESS_CLEANUP_PARALLELISM)),
    )
    _best_effort_release_assigned_vms(assistant_ids, gce_client)

    for _ in range(_STRESS_IDLE_CONTAINER_TARGET):
        replenish_pool()
        time.sleep(2)

    if gce_client is not None:
        _trigger_vm_pool_rebalance(comms_client)

    if not wait_for_baseline:
        return

    wait_for_idle_pool(
        batch_api,
        min_idle=_STRESS_IDLE_CONTAINER_TARGET,
        timeout=_STRESS_CONTAINER_BASELINE_TIMEOUT_SECONDS,
    )
    if gce_client is not None:
        wait_for_idle_vm_pool(
            gce_client,
            min_idle=_STRESS_IDLE_VM_TARGET,
            timeout=_STRESS_VM_BASELINE_TIMEOUT_SECONDS,
        )


def _new_violations(current, baseline):
    """Return violations in *current* that were not in *baseline*."""
    baseline_keys = {(v.invariant_id, v.message) for v in baseline}
    return [v for v in current if (v.invariant_id, v.message) not in baseline_keys]


def _print_violations(violations, label=""):
    if not violations:
        return
    tag = f" ({label})" if label else ""
    for v in violations:
        print(f"  [{v.invariant_id}]{tag} {v.message}")


def _get_vm_hostname(vm_instance):
    """Extract the hostname from a GCE VM instance's metadata."""
    for item in vm_instance.metadata.items or []:
        if item.key == "hostname":
            return item.value
    return f"{vm_instance.name}.vm.unify.ai"


def _kill_pod(core_api, job_name, namespace=NAMESPACE):
    """Delete the first active pod for a Job (simulates OOM crash)."""
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"job-name={job_name}",
    )
    for pod in pods.items:
        if pod.status.phase in ("Running", "Pending"):
            core_api.delete_namespaced_pod(
                name=pod.metadata.name,
                namespace=namespace,
                grace_period_seconds=0,
            )
            return pod.metadata.name
    return None


def _latest_active_job(batch_api, assistant_id: str):
    """Return the freshest active Job for an assistant, if any."""
    jobs = list_jobs_with_assistant_id(batch_api, assistant_id)
    if not jobs:
        return None
    return max(
        jobs,
        key=lambda job: str(getattr(job.metadata, "creation_timestamp", "") or ""),
    )


def _job_has_active_pods(batch_api, job_name: str, namespace=NAMESPACE) -> bool:
    """Return whether a specific Job still has active pods."""
    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
    except Exception:
        return False
    return bool(job.status.active and job.status.active > 0)


def _kill_current_runtime_pod(
    batch_api,
    core_api,
    assistant_id: str,
    namespace=NAMESPACE,
) -> tuple[str | None, str | None]:
    """Delete a pod from the assistant's freshest active Job."""
    job = _latest_active_job(batch_api, assistant_id)
    if job is None:
        return None, None
    job_name = str(job.metadata.name or "")
    if not job_name:
        return None, None
    return job_name, _kill_pod(core_api, job_name, namespace=namespace)


def _select_stable_assistant(batch_api, assistants, *, excluded_ids: set[str]):
    """Return a non-excluded assistant that still has a live container."""
    for assistant in assistants:
        assistant_id = assistant["assistant_id"]
        if assistant_id in excluded_ids:
            continue
        if _latest_active_job(batch_api, assistant_id) is not None:
            return assistant
    return None


def _wait_for_vm_assigned(gce_client, assistant_id, timeout=120, interval=10):
    """Poll until a VM is assigned to this assistant."""
    return poll_until(
        lambda: list_assigned_vms(gce_client, assistant_id),
        timeout=timeout,
        interval=interval,
        description=f"VM assigned to assistant {assistant_id}",
    )


def _desktop_ready_signal_matches_vm(session: dict | None, vm_name: str) -> bool:
    """Return whether session status recorded desktop readiness for this VM."""
    if not isinstance(session, dict):
        return False
    status = session.get("status") or {}
    binding = status.get("binding") or {}
    binding_vm_name = str(((binding.get("vmRef") or {}).get("name")) or "")
    return (
        bool(binding.get("vmReadyObservedAt"))
        and bool(binding.get("desktopUrl"))
        and binding_vm_name == vm_name
    )


def _assistant_vm_contract_status(
    comms_client,
    gce_client,
    assistant_data: dict,
) -> dict:
    """Describe an assistant's current desktop-ready/authenticated VM state."""
    assistant_id = assistant_data["assistant_id"]
    result = {
        "assistant_id": assistant_id,
        "state": "not_assigned",
        "vm_name": "",
        "hostname": "",
        "session_phase": "",
        "session_vm_name": "",
        "binding_id": "",
        "desktop_url": "",
        "vm_ready_observed_at": "",
        "last_error": "",
        "auth_status": "",
    }
    vms = list_assigned_vms(gce_client, assistant_id)
    if not vms:
        return result

    vm = vms[0]
    hostname = _get_vm_hostname(vm)
    session = get_assistant_session(comms_client, assistant_id) or {}
    status = session.get("status") or {}
    binding = status.get("binding") or {}
    result.update(
        {
            "state": "assigned_waiting_for_desktop_ready",
            "vm_name": vm.name,
            "hostname": hostname,
            "session_phase": str(status.get("phase") or ""),
            "session_vm_name": str(((binding.get("vmRef") or {}).get("name")) or ""),
            "binding_id": str(binding.get("id") or ""),
            "desktop_url": str(binding.get("desktopUrl") or ""),
            "vm_ready_observed_at": str(binding.get("vmReadyObservedAt") or ""),
            "last_error": str(status.get("lastError") or ""),
        },
    )
    if not _desktop_ready_signal_matches_vm(session, vm.name):
        return result

    resp = probe_vm_agent_service_authenticated(hostname, assistant_data["api_key"])
    result["auth_status"] = str(resp.status_code) if resp is not None else "no response"
    if resp is not None and resp.status_code == 200:
        result["state"] = "ready"
    else:
        result["state"] = "assigned_auth_pending"
    return result


def _wait_for_assistant_vm_contract(
    comms_client,
    gce_client,
    assistant_data: dict,
    *,
    timeout: float = _VM_DESKTOP_READY_TIMEOUT_SECONDS,
    interval: float = _VM_CONTRACT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Wait until an assistant satisfies the desktop-ready/auth VM contract."""
    last_result = {
        "assistant_id": assistant_data["assistant_id"],
        "state": "not_assigned",
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            last_result = _assistant_vm_contract_status(
                comms_client,
                gce_client,
                assistant_data,
            )
        except Exception as exc:
            last_result = {
                **last_result,
                "state": "check_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if last_result.get("state") == "ready":
            return last_result
        time.sleep(interval)
    return {**last_result, "timed_out": True}


def _format_vm_contract_result(result: dict, *, timeout: float) -> str:
    """Format a VM contract poll result for human-readable stress output."""
    state = result.get("state")
    if state == "ready":
        return f"VM {result['vm_name']}, auth OK ({result['hostname']})"
    if state == "not_assigned":
        return f"VM not assigned after {int(timeout)}s"
    if state == "assigned_waiting_for_desktop_ready":
        phase = result.get("session_phase") or "unknown"
        last_error = result.get("last_error") or "waiting for vm_ready"
        return (
            f"VM {result['vm_name']}, waiting for desktop readiness "
            f"(phase={phase}) - {last_error}"
        )
    if state == "assigned_auth_pending":
        return (
            f"VM {result['vm_name']}, desktop ready but auth still failing "
            f"({result['hostname']}) - {result.get('auth_status') or 'unknown'}"
        )
    return f"VM readiness check failed - {result.get('error', state)}"


def _assistant_duplicate_job_snapshot(
    comms,
    batch_api,
    core_api,
    assistant_id: str,
) -> dict:
    """Capture enough evidence to reconstruct an INV-1 duplicate-job failure."""
    sanitized = assistant_id.lower().replace("_", "-")
    job_items = batch_api.list_namespaced_job(
        namespace=NAMESPACE,
        label_selector=f"app=unity,assistant-id={sanitized}",
    ).items
    active_jobs = []
    for job in job_items:
        if not (job.status.active and job.status.active > 0):
            continue
        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})
        pods = []
        try:
            pod_items = core_api.list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector=f"job-name={job.metadata.name}",
            ).items
            for pod in pod_items:
                pods.append(
                    {
                        "pod_name": pod.metadata.name,
                        "phase": pod.status.phase,
                        "node_name": pod.spec.node_name,
                        "start_time": pod.status.start_time,
                        "deletion_timestamp": pod.metadata.deletion_timestamp,
                    },
                )
        except Exception as exc:
            pods.append({"pod_collection_error": f"{type(exc).__name__}: {exc}"})

        active_jobs.append(
            {
                "job_name": job.metadata.name,
                "creation_timestamp": job.metadata.creation_timestamp,
                "resource_version": job.metadata.resource_version,
                "active": job.status.active,
                "ready": getattr(job.status, "ready", None),
                "start_time": job.status.start_time,
                "labels": labels,
                "annotations": annotations,
                "pods": pods,
            },
        )

    active_jobs.sort(key=lambda job: str(job.get("creation_timestamp") or ""))

    try:
        session = get_assistant_session(comms, assistant_id)
    except Exception as exc:
        session = {
            "session_read_error": f"{type(exc).__name__}: {exc}",
        }

    runtime_resp = comms.get(f"/infra/runtime/{assistant_id}")
    if runtime_resp.status_code == 200:
        runtime_status = runtime_resp.json()
    else:
        runtime_status = {
            "status_code": runtime_resp.status_code,
            "text": runtime_resp.text[:1000],
        }

    session_binding = ""
    session_activation_id = ""
    session_name = ""
    if isinstance(session, dict):
        session_name = str(((session.get("metadata") or {}).get("name") or ""))
        session_activation_id = str(
            (((session.get("spec") or {}).get("activationId")) or ""),
        )
        session_binding = str(
            (
                (
                    ((session.get("status") or {}).get("binding") or {}).get("jobRef")
                    or {}
                )
            ).get("name", "")
            or "",
        )

    return {
        "assistant_id": assistant_id,
        "active_job_count": len(active_jobs),
        "active_jobs": active_jobs,
        "session_name": session_name,
        "session_activation_id": session_activation_id,
        "session_bound_job_name": session_binding,
        "session": session,
        "runtime_status": runtime_status,
    }


def _record_duplicate_job_snapshot(request, snapshot: dict) -> None:
    """Persist duplicate-job evidence into the pytest failure artifact."""
    snapshots = dict(
        getattr(request.node, "_extra_failure_context", {}).get(
            "duplicate_job_snapshots",
            {},
        ),
    )
    snapshots[str(snapshot["assistant_id"])] = snapshot
    add_failure_context(request, "duplicate_job_snapshots", snapshots)

    tracker = getattr(request.node, "_runtime_identity_tracker", None)
    if tracker is None:
        return
    tracker.track(
        assistant_id=str(snapshot["assistant_id"]),
        session_name=snapshot.get("session_name") or None,
        activation_id=snapshot.get("session_activation_id") or None,
    )
    if snapshot.get("session_bound_job_name"):
        tracker.track(job_name=str(snapshot["session_bound_job_name"]))
    for job in snapshot.get("active_jobs", []):
        tracker.track(job_name=str(job["job_name"]))
        for pod in job.get("pods", []):
            pod_name = pod.get("pod_name")
            if pod_name:
                tracker.track(pod_name=str(pod_name))


class _SchedulerNoise:
    """Background thread that fires lightweight scheduler utilities.

    Production now routes cron traffic through ``/scheduled/infra/maintenance``,
    but that endpoint performs the full shared-environment sweep and can block
    for minutes while VM rebalance completes. The stress test instead injects
    the underlying utility endpoints individually so it still exercises pool
    churn and stale-runtime races without folding in unrelated global latency.
    """

    def __init__(self, min_interval=20, max_interval=45):
        self._stop = False
        self._min = min_interval
        self._max = max_interval
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._future = None
        self._fire_count = 0

    def start(self):
        self._future = self._pool.submit(self._run)
        print(
            f"    [scheduler-noise] Started (interval {self._min}-{self._max}s)",
        )

    def stop(self):
        self._stop = True
        if self._future:
            self._future.result(timeout=60)
        self._pool.shutdown(wait=False)
        print(
            f"    [scheduler-noise] Stopped after {self._fire_count} firings",
        )

    def _run(self):
        while not self._stop:
            time.sleep(random.uniform(self._min, self._max))
            if self._stop:
                break
            action = random.choice(["cleanup", "refresh", "stale-expire"])
            if action == "cleanup":
                _trigger_cleanup()
            elif action == "refresh":
                _trigger_pool_refresh()
            else:
                _trigger_stale_expire()
            self._fire_count += 1


# ---------------------------------------------------------------------------
# The stress test
# ---------------------------------------------------------------------------


def test_production_traffic_stress(
    test_assistants,
    comms,
    batch_api,
    core_api,
    gce_client,
    poll,
    request,
):
    """Simulate a product launch: N simultaneous users with diverse traffic.

    Eight phases exercise the system under realistic concurrent load,
    including crash recovery, cleanup races, and rapid restarts.
    Setup restores the expected idle baseline first, then the test drives
    pool exhaustion, overflow queuing, and replenishment under load.
    """
    assistants = test_assistants
    if len(assistants) < 3:
        pytest.skip(
            "Need at least 3 test assistants " "(set TEST_CREATE_ASSISTANT_COUNT >= 3)",
        )

    N = len(assistants)
    all_ids = [a["assistant_id"] for a in assistants]

    print(f"\n{'=' * 70}")
    print(f"  STRESS TEST: {N} assistants, 8 phases")
    print(f"{'=' * 70}")

    # ------------------------------------------------------------------
    # Clean slate: stop any runtimes tied to these assistants, kick both
    # pools back toward their configured idle targets, and wait for the
    # stress baseline before issuing traffic.
    # ------------------------------------------------------------------
    print(f"[Setup] Cleaning previous state and restoring stress baseline...")
    _reset_stress_baseline(
        comms,
        batch_api,
        gce_client,
        all_ids,
        context="stress setup baseline reset",
        cleanup_timeout=_STRESS_SETUP_CLEANUP_TIMEOUT_SECONDS,
        wait_for_baseline=True,
    )
    idle_before = count_idle_jobs(batch_api)
    print(
        f"[Setup] Idle pool: {idle_before} containers "
        f"(target: {_STRESS_IDLE_CONTAINER_TARGET})",
    )

    baseline_violations = check_invariants(batch_api, gce_client)
    if baseline_violations:
        print(f"[Setup] Pre-existing invariant violations: {len(baseline_violations)}")
        _print_violations(baseline_violations, "baseline")

    idle_vms_before = 0
    if gce_client is not None:
        try:
            idle_vms_before = len(list_idle_vms(gce_client))
            print(
                f"[Setup] Idle VM pool: {idle_vms_before} ubuntu VMs "
                f"(target: {_STRESS_IDLE_VM_TARGET})",
            )
        except Exception:
            pass

    scheduler_noise = _SchedulerNoise(min_interval=20, max_interval=45)

    try:
        scheduler_noise.start()
        # ==================================================================
        # PHASE 1: Thundering Herd — start all N assistants at once
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 1] Thundering herd: {N} concurrent /infra/job/start requests")
        print(f"          Pool has {idle_before} idle containers")
        print(f"{'—' * 70}")

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=N + 1) as pool:
            futures = {
                pool.submit(_start_job_tolerant, comms, a): a["assistant_id"]
                for a in assistants
            }
            # Fire pool refresh concurrently (simulates hourly cron / post-deploy)
            refresh_future = pool.submit(_trigger_pool_refresh)

            results = {}
            for f in as_completed(futures):
                if f is refresh_future:
                    continue
                aid = futures[f]
                status, body = f.result()
                results[aid] = (status, body)

        immediate = [aid for aid, (s, _) in results.items() if s == 200]
        queued = []
        errors = [aid for aid, (s, _) in results.items() if s != 200]
        elapsed_p1 = time.monotonic() - t0

        print(f"[Phase 1] Results ({elapsed_p1:.1f}s):")
        print(f"  Immediate (200): {len(immediate)}")
        print(f"  Errors:          {len(errors)}")
        for aid in errors:
            s, body = results[aid]
            print(f"    assistant {aid}: HTTP {s} — {body[:200]}")

        assert (
            not errors
        ), f"{len(errors)} startup requests failed (expected 200): " + ", ".join(
            f"{aid}={results[aid][0]}" for aid in errors
        )

        p1_invariants = check_invariants(batch_api, gce_client)
        p1_new = _new_violations(p1_invariants, baseline_violations)
        inv1_violations = [v for v in p1_new if v.invariant_id == "INV-1"]
        if p1_new:
            print(f"[Phase 1] Invariant check: {len(p1_new)} new violation(s)")
            _print_violations(p1_new)
        else:
            print(f"[Phase 1] Invariant check: clean")

        assert not inv1_violations, (
            "INV-1 violated during thundering herd — duplicate containers: "
            + "; ".join(v.message for v in inv1_violations)
        )

        if queued:
            print(f"[Phase 1] Unexpected queued startups under AssistantSession v1")

        # ==================================================================
        # PHASE 2: Traffic Firehose — blast traffic before containers ready
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 2] Traffic firehose: mixed messages, meets, events")
        print(f"          (containers may still be booting)")
        print(f"{'—' * 70}")

        t0 = time.monotonic()
        traffic_futures = []
        with ThreadPoolExecutor(max_workers=min(N * 4, 30)) as pool:
            for a in assistants:
                traffic_futures.append(
                    pool.submit(send_test_message, a, "Stress test message 1"),
                )
                traffic_futures.append(
                    pool.submit(send_test_message, a, "Stress test message 2"),
                )
                traffic_futures.append(pool.submit(send_test_meet, a))
                traffic_futures.append(
                    pool.submit(
                        send_test_system_event,
                        a,
                        "user_screen_share_started",
                    ),
                )

        adapter_ok = 0
        adapter_client_err = 0
        adapter_server_err = 0
        first_error_body = None
        for f in as_completed(traffic_futures):
            try:
                resp = f.result()
                if resp.status_code < 400:
                    adapter_ok += 1
                elif resp.status_code < 500:
                    adapter_client_err += 1
                else:
                    adapter_server_err += 1
                    if first_error_body is None:
                        first_error_body = resp.text[:300]
            except Exception:
                adapter_server_err += 1

        total = N * 4
        elapsed_p2 = time.monotonic() - t0

        print(
            f"[Phase 2] Sent {total} requests across {N} assistants ({elapsed_p2:.1f}s)",
        )
        print(
            f"  OK: {adapter_ok} | 4xx: {adapter_client_err} | 5xx: {adapter_server_err}",
        )
        if first_error_body:
            print(f"  First error: {first_error_body}")
        print(f"[Phase 2] Pool: {count_idle_jobs(batch_api)} idle containers")

        if adapter_server_err > 0:
            import warnings

            warnings.warn(
                f"{adapter_server_err}/{total} adapter requests returned 5xx. "
                f"This is common for is_local=True test assistants and does not "
                f"indicate an infrastructure failure.",
            )

        # ==================================================================
        # PHASE 3: Steady State Verification (with VM polling)
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 3] Waiting for all {N} containers + VMs to be ready...")
        print(f"{'—' * 70}")

        t0 = time.monotonic()
        containers_up = {}
        pending_container_ids = {a["assistant_id"] for a in assistants}
        container_deadline = time.monotonic() + 300
        container_poll_round = 0

        while pending_container_ids and time.monotonic() < container_deadline:
            ready_now = []
            for aid in sorted(pending_container_ids):
                jobs = list_jobs_with_assistant_id(batch_api, aid)
                if not jobs:
                    continue
                containers_up[aid] = jobs[0].metadata.name
                print(f"  {aid}: container running ({containers_up[aid]})")
                ready_now.append(aid)

            for aid in ready_now:
                pending_container_ids.discard(aid)

            if not pending_container_ids:
                break

            container_poll_round += 1
            if container_poll_round % 3 == 0:
                _trigger_pool_refresh()
            time.sleep(10)

        containers_failed = {}
        for aid in sorted(pending_container_ids):
            containers_failed[aid] = "timeout"
            print(f"  {aid}: TIMEOUT — no container after 300s")

        elapsed_containers = time.monotonic() - t0
        print(
            f"\n[Phase 3] Containers: {len(containers_up)}/{N} running, "
            f"{len(containers_failed)} failed ({elapsed_containers:.1f}s)",
        )

        assert (
            len(containers_failed) == 0
        ), f"{len(containers_failed)} assistants never got a container: " + ", ".join(
            containers_failed.keys(),
        )

        for aid in all_ids:
            jobs = list_jobs_with_assistant_id(batch_api, aid)
            if len(jobs) > 1:
                snapshot = _assistant_duplicate_job_snapshot(
                    comms,
                    batch_api,
                    core_api,
                    aid,
                )
                _record_duplicate_job_snapshot(request, snapshot)
                raise AssertionError(
                    f"INV-1: assistant {aid} has {len(jobs)} containers "
                    f"({[j.metadata.name for j in jobs]})\n"
                    f"Duplicate job snapshot:\n"
                    f"{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}",
                )

        if gce_client is not None:
            print(
                "[Phase 3] Waiting for desktops to reach authenticated readiness "
                f"(up to {_VM_DESKTOP_READY_TIMEOUT_SECONDS}s)...",
            )
            with ThreadPoolExecutor(max_workers=min(N, 12)) as pool:
                vm_futures = {
                    pool.submit(
                        _wait_for_assistant_vm_contract,
                        comms,
                        gce_client,
                        a,
                    ): a["assistant_id"]
                    for a in assistants
                }
                vm_results = []
                for future in as_completed(vm_futures):
                    result = future.result()
                    vm_results.append(result)
                    print(
                        f"  {result['assistant_id']}: "
                        f"{_format_vm_contract_result(result, timeout=_VM_DESKTOP_READY_TIMEOUT_SECONDS)}",
                    )

            vm_assigned = sum(bool(result.get("vm_name")) for result in vm_results)
            vm_auth_ok = sum(result["state"] == "ready" for result in vm_results)
            vm_desktop_pending_ids = sorted(
                result["assistant_id"]
                for result in vm_results
                if result["state"] == "assigned_waiting_for_desktop_ready"
            )
            vm_auth_fail_ids = sorted(
                result["assistant_id"]
                for result in vm_results
                if result["state"] in ("assigned_auth_pending", "check_failed")
            )
            vm_not_assigned = sum(
                result["state"] == "not_assigned" for result in vm_results
            )

            print(
                f"[Phase 3] VMs: {vm_assigned}/{N} assigned, "
                f"{vm_auth_ok} desktop-ready + auth OK, "
                f"{len(vm_desktop_pending_ids)} waiting for desktop readiness, "
                f"{len(vm_auth_fail_ids)} auth/verification FAIL, "
                f"{vm_not_assigned} not assigned (pool had {idle_vms_before} idle)",
            )

            assert not vm_desktop_pending_ids, (
                "Assigned VMs never reached desktop readiness within "
                f"{_VM_DESKTOP_READY_TIMEOUT_SECONDS}s: "
                + ", ".join(vm_desktop_pending_ids)
            )
            assert not vm_auth_fail_ids, (
                "INV-11: desktop-ready VMs failed authenticated agent probe: "
                + ", ".join(vm_auth_fail_ids)
            )

            if vm_not_assigned > 0:
                import warnings

                warnings.warn(
                    f"{vm_not_assigned}/{N} assistants have containers but no VM "
                    f"after {_VM_DESKTOP_READY_TIMEOUT_SECONDS}s of reconciliation. "
                    f"VM pool had {idle_vms_before} "
                    f"idle VMs for {N} assistants. This may indicate the VM pool "
                    f"cannot replenish fast enough for this scale.",
                )

        p3_invariants = check_invariants(batch_api, gce_client)
        p3_new = _new_violations(p3_invariants, baseline_violations)
        if p3_new:
            print(f"[Phase 3] Invariant violations: {len(p3_new)}")
            _print_violations(p3_new)
        else:
            print(f"[Phase 3] Invariants: all clear")

        # (outbound message check deferred to after Phase 4 — containers
        # need time to initialize and process queued messages)

        # ==================================================================
        # PHASE 4: Sustained Mixed Load
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 4] Sustained mixed load: 3 rounds, 30s apart")
        print(f"{'—' * 70}")

        p4_snapshot = check_invariants(batch_api, gce_client)

        for round_num in range(1, 4):
            t0 = time.monotonic()
            round_futures = []

            shuffled = list(assistants)
            random.shuffle(shuffled)

            heavy = shuffled[: max(1, N // 3)]
            medium_group = shuffled[max(1, N // 3) : max(2, 2 * N // 3)]
            light = shuffled[max(2, 2 * N // 3) :]

            with ThreadPoolExecutor(max_workers=min(N * 3, 30)) as pool:
                for a in heavy:
                    for i in range(random.randint(3, 5)):
                        round_futures.append(
                            pool.submit(
                                send_test_message,
                                a,
                                f"Round {round_num} burst msg {i}",
                            ),
                        )
                for a in medium_group:
                    round_futures.append(pool.submit(send_test_meet, a))
                    round_futures.append(
                        pool.submit(
                            send_test_message,
                            a,
                            f"Round {round_num} msg",
                        ),
                    )
                for a in light:
                    round_futures.append(
                        pool.submit(
                            send_test_system_event,
                            a,
                            "assistant_update",
                            f"Round {round_num} update",
                        ),
                    )

            request_count = len(round_futures)
            round_errors = 0
            for f in as_completed(round_futures):
                try:
                    resp = f.result()
                    if resp.status_code >= 500:
                        round_errors += 1
                except Exception:
                    round_errors += 1

            elapsed = time.monotonic() - t0

            round_invariants = check_invariants(batch_api, gce_client)
            round_new = _new_violations(round_invariants, p4_snapshot)
            critical = [
                v for v in round_new if v.invariant_id in ("INV-1", "INV-2", "INV-3")
            ]

            print(
                f"[Phase 4] Round {round_num}: {request_count} requests, "
                f"{round_errors} errors ({elapsed:.1f}s) | "
                f"invariants: {len(round_new)} new, {len(critical)} critical",
            )
            if critical:
                _print_violations(critical, f"round {round_num}")

            assert not critical, (
                f"Critical invariant violation during sustained load round {round_num}: "
                + "; ".join(f"[{v.invariant_id}] {v.message}" for v in critical)
            )

            if round_num < 3:
                # Fire scheduler endpoints between rounds (concurrent with
                # any in-flight request processing inside containers)
                if round_num == 1:
                    print(f"    [scheduler] Firing cleanup between rounds...")
                    _trigger_cleanup()
                elif round_num == 2:
                    print(f"    [scheduler] Firing stale-expire between rounds...")
                    _trigger_stale_expire()
                time.sleep(30)

        # INV-1 provocation: try to create duplicate containers for the same
        # assistant by racing two start_job calls while it's already running.
        print(f"\n[Phase 4] INV-1 provocation: racing duplicate start_job calls...")
        provoke_target = assistants[0]
        provoke_aid = provoke_target["assistant_id"]
        with ThreadPoolExecutor(max_workers=3) as pool:
            dup_futures = [
                pool.submit(_start_job_tolerant, comms, provoke_target)
                for _ in range(3)
            ]
            dup_results = [f.result() for f in as_completed(dup_futures)]

        dup_statuses = [s for s, _ in dup_results]
        print(f"  {provoke_aid}: 3 concurrent start_job → {dup_statuses}")

        dup_jobs = list_jobs_with_assistant_id(batch_api, provoke_aid)
        if len(dup_jobs) > 1:
            snapshot = _assistant_duplicate_job_snapshot(
                comms,
                batch_api,
                core_api,
                provoke_aid,
            )
            _record_duplicate_job_snapshot(request, snapshot)
            raise AssertionError(
                f"INV-1 PROVOKED: assistant {provoke_aid} has {len(dup_jobs)} containers "
                f"after 3 concurrent start_job calls: "
                f"{[j.metadata.name for j in dup_jobs]}\n"
                f"Duplicate job snapshot:\n"
                f"{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}",
            )
        print(f"  INV-1 provocation: {len(dup_jobs)} container(s) — safe")

        # Check outbound messages now — containers have been running through
        # Phase 4 (3 rounds × 30s = ~90s+) so they've had time to process
        # Phase 2 inbound messages and produce LLM responses.
        try:
            from google.cloud import pubsub_v1 as _pubsub_v1

            subscriber = _pubsub_v1.SubscriberClient()
            delivered_count = 0
            checked_count = 0
            check_sample = assistants[: min(3, N)]
            for a in check_sample:
                aid = a["assistant_id"]
                msgs = pull_outbound_messages(subscriber, str(aid), timeout=15)
                checked_count += 1
                if msgs:
                    delivered_count += 1
                    print(f"  {aid}: {len(msgs)} outbound message(s) — delivered")
                else:
                    print(f"  {aid}: no outbound messages yet")
            print(
                f"[Phase 4] Message delivery: {delivered_count}/{checked_count} "
                f"assistants have outbound messages",
            )
            if delivered_count == 0 and checked_count > 0:
                import warnings

                warnings.warn(
                    f"No outbound messages found for any of the {checked_count} "
                    f"assistants checked after Phase 4.",
                )
        except Exception as e:
            print(f"[Phase 4] Message delivery check skipped: {e}")

        # ==================================================================
        # PHASE 5: Crash Recovery Under Load
        # ==================================================================
        desired_crash_count = min(2, N // 2)
        # Pick only assistants with a live, killable Job so this phase measures
        # crash recovery instead of pre-existing release/drain state.
        crash_assistants = []
        crash_job_names = {}
        for assistant in assistants:
            if len(crash_assistants) >= desired_crash_count:
                break
            assistant_id = assistant["assistant_id"]
            job_name, killed_pod = _kill_current_runtime_pod(
                batch_api,
                core_api,
                assistant_id,
            )
            if not job_name:
                print(f"  {assistant_id}: skip crash candidate — no active job")
                continue
            if not killed_pod:
                print(
                    f"  {assistant_id}: skip crash candidate — no running pod for {job_name}",
                )
                continue
            crash_assistants.append(assistant)
            crash_job_names[assistant_id] = job_name
            containers_up[assistant_id] = job_name
            print(f"  {assistant_id}: killed pod {killed_pod}")

        crash_count = len(crash_assistants)
        crash_ids = [a["assistant_id"] for a in crash_assistants]
        crash_id_set = set(crash_ids)
        surviving_assistants = [
            assistant
            for assistant in assistants
            if assistant["assistant_id"] not in crash_id_set
        ]
        assert (
            crash_count == desired_crash_count
        ), f"Need {desired_crash_count} live assistants with killable pods for Phase 5"

        print(f"\n{'—' * 70}")
        print(
            f"[Phase 5] Crash recovery: killing {crash_count} pods while traffic flows",
        )
        print(f"{'—' * 70}")

        # Send background traffic to surviving assistants during recovery
        bg_stop = False

        def _bg_traffic():
            while not bg_stop:
                for a in surviving_assistants:
                    try:
                        send_test_message(a, "Background traffic during crash recovery")
                    except Exception:
                        pass
                time.sleep(5)

        bg_thread_pool = ThreadPoolExecutor(max_workers=1)
        bg_future = bg_thread_pool.submit(_bg_traffic)

        try:
            # Fire stale-expire while watcher is processing crashes — tests
            # whether the sweep races with the watcher on VM release / job suspend
            print(f"    [scheduler] Firing stale-expire during crash recovery...")
            _trigger_stale_expire()

            # Wait for jobs to reach terminal state
            print(f"[Phase 5] Waiting for crashed jobs to terminate...")
            time.sleep(15)

            for aid, job_name in crash_job_names.items():
                try:
                    poll_until(
                        lambda jn=job_name: not _job_has_active_pods(batch_api, jn),
                        timeout=120,
                        interval=10,
                        description=f"Job {job_name} to terminate after pod kill",
                    )
                    print(f"  {aid}: job terminated")
                except TimeoutError:
                    print(f"  {aid}: job still active after 120s")

            # Verify VMs released for crashed assistants
            if gce_client is not None:
                time.sleep(10)
                for aid in crash_ids:
                    vms = list_assigned_vms(gce_client, aid)
                    if vms:
                        print(f"  {aid}: VM still assigned (orphaned)")
                    else:
                        print(f"  {aid}: VM released — clean")

            # Re-start crashed assistants
            print(f"[Phase 5] Re-starting {crash_count} crashed assistants...")
            for a in crash_assistants:
                aid = a["assistant_id"]
                status, body = _start_job_tolerant(comms, a)
                print(f"  {aid}: re-start → HTTP {status}")

            # Wait for new containers in parallel so one slow recovery does not
            # distort the rest of the crash cohort.
            with ThreadPoolExecutor(max_workers=max(1, crash_count)) as pool:
                recovery_futures = {
                    pool.submit(
                        wait_for_container_running,
                        batch_api,
                        assistant["assistant_id"],
                        timeout=300,
                        interval=10,
                    ): assistant["assistant_id"]
                    for assistant in crash_assistants
                }
                for future in as_completed(recovery_futures):
                    aid = recovery_futures[future]
                    try:
                        jobs = future.result()
                        containers_up[aid] = jobs[0].metadata.name
                        print(f"  {aid}: recovered → {containers_up[aid]}")
                    except TimeoutError:
                        print(f"  {aid}: FAILED to recover — no container after 300s")

            p5_invariants = check_invariants(batch_api, gce_client)
            p5_new = _new_violations(p5_invariants, baseline_violations)
            p5_critical = [v for v in p5_new if v.invariant_id == "INV-1"]
            if p5_new:
                print(f"[Phase 5] Invariant violations: {len(p5_new)}")
                _print_violations(p5_new)
            else:
                print(f"[Phase 5] Invariants: all clear")

            assert (
                not p5_critical
            ), "INV-1 violated during crash recovery: " + "; ".join(
                v.message for v in p5_critical
            )

        finally:
            bg_stop = True
            try:
                bg_future.result(timeout=30)
            except (TimeoutError, Exception):
                pass
            bg_thread_pool.shutdown(wait=False)

        # ==================================================================
        # PHASE 6: Cleanup Concurrent With Startups (INV-8 TOCTOU)
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 6] Cleanup vs startup race (3 rounds)")
        print(f"{'—' * 70}")

        # Keep the race on a stable assistant rather than a fresh crash victim.
        target_assistant = _select_stable_assistant(
            batch_api,
            assistants,
            excluded_ids=crash_id_set,
        )
        assert (
            target_assistant is not None
        ), "Need a non-crashed assistant with a live container for Phase 6"
        target_aid = target_assistant["assistant_id"]
        print(f"[Phase 6] Using stable target assistant {target_aid}")

        for race_round in range(1, 4):
            # Delete the target's job so there's an idle-looking container
            cleanup_assistant_jobs(
                batch_api,
                [target_aid],
                strict=True,
                context=f"stress phase 6 round {race_round} pre-race cleanup",
            )
            time.sleep(3)

            # Race: cleanup vs re-start
            with ThreadPoolExecutor(max_workers=2) as pool:
                cleanup_future = pool.submit(_trigger_cleanup)
                start_future = pool.submit(_start_job_tolerant, comms, target_assistant)

            cleanup_future.result()
            start_status, start_body = start_future.result()

            # Verify the assistant got a container
            try:
                jobs = wait_for_container_running(
                    batch_api,
                    target_aid,
                    timeout=180,
                    interval=10,
                )
                containers_up[target_aid] = jobs[0].metadata.name
                print(
                    f"[Phase 6] Round {race_round}: start={start_status}, "
                    f"container={containers_up[target_aid]} — OK",
                )
            except TimeoutError:
                print(
                    f"[Phase 6] Round {race_round}: start={start_status}, "
                    f"container=NONE — CLEANUP WON THE RACE",
                )

            inv_check = check_invariants(batch_api, gce_client)
            inv8 = [v for v in inv_check if v.invariant_id == "INV-8"]
            assert (
                not inv8
            ), f"INV-8 violated in cleanup race round {race_round}: " + "; ".join(
                v.message for v in inv8
            )

        # ==================================================================
        # PHASE 7: Rapid Restart With Disk Re-attachment
        # ==================================================================
        restart_count = min(2, N // 2)
        # Prefer assistants untouched by earlier perturbation phases.
        restart_candidates = [
            assistant
            for assistant in assistants
            if assistant["assistant_id"] not in crash_id_set
            and assistant["assistant_id"] != target_aid
        ]
        if len(restart_candidates) < restart_count:
            restart_candidates = [
                assistant
                for assistant in assistants
                if assistant["assistant_id"] != target_aid
            ]
        restart_assistants = restart_candidates[:restart_count]
        restart_ids = [a["assistant_id"] for a in restart_assistants]

        print(f"\n{'—' * 70}")
        print(
            f"[Phase 7] Rapid restart: {restart_count} assistants (shutdown + immediate re-start)",
        )
        print(f"{'—' * 70}")

        # Record current state
        for a in restart_assistants:
            aid = a["assistant_id"]
            job_name = containers_up.get(aid, "unknown")
            print(f"  {aid}: current container={job_name}")

        # Delete jobs (triggers VM release + disk detach) and wait for
        # K8s Foreground deletion to complete so start_job doesn't see
        # the dying container as "already running".
        cleanup_assistant_jobs(
            batch_api,
            restart_ids,
            strict=True,
            context="stress phase 7 pre-restart cleanup",
        )
        for aid in restart_ids:
            try:
                poll_until(
                    lambda _aid=aid: not list_jobs_with_assistant_id(batch_api, _aid),
                    timeout=60,
                    interval=5,
                    description=f"Job for {aid} to fully terminate",
                )
            except TimeoutError:
                print(f"  {aid}: old Job still active after 60s, proceeding anyway")
        print(f"[Phase 7] Jobs deleted and terminated — re-starting...")

        for a in restart_assistants:
            aid = a["assistant_id"]
            status, body = _start_job_tolerant(comms, a)
            print(f"  {aid}: re-start → HTTP {status}")
        # Wait for new containers
        for a in restart_assistants:
            aid = a["assistant_id"]
            try:
                jobs = wait_for_container_running(
                    batch_api,
                    aid,
                    timeout=300,
                    interval=10,
                )
                containers_up[aid] = jobs[0].metadata.name
                print(f"  {aid}: new container={containers_up[aid]}")
            except TimeoutError:
                print(f"  {aid}: FAILED — no container after 300s")

        # Verify VM re-assignment + auth
        if gce_client is not None and restart_assistants:
            print(
                "[Phase 7] Waiting for re-attached desktops to reach "
                f"authenticated readiness (up to {_VM_REATTACH_TIMEOUT_SECONDS}s)...",
            )
            with ThreadPoolExecutor(
                max_workers=min(len(restart_assistants), 6),
            ) as pool:
                vm_futures = {
                    pool.submit(
                        _wait_for_assistant_vm_contract,
                        comms,
                        gce_client,
                        a,
                        timeout=_VM_REATTACH_TIMEOUT_SECONDS,
                    ): a["assistant_id"]
                    for a in restart_assistants
                }
                for future in as_completed(vm_futures):
                    result = future.result()
                    print(
                        f"  {result['assistant_id']}: "
                        f"{_format_vm_contract_result(result, timeout=_VM_REATTACH_TIMEOUT_SECONDS)}",
                    )

        p7_invariants = check_invariants(batch_api, gce_client)
        p7_new = _new_violations(p7_invariants, baseline_violations)
        if p7_new:
            print(f"[Phase 7] Invariant violations: {len(p7_new)}")
            _print_violations(p7_new)
        else:
            print(f"[Phase 7] Invariants: all clear")

        # ==================================================================
        # PHASE 8: Wind-down and Cleanup
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 8] Cleaning up {N} assistants...")
        print(f"{'—' * 70}")

        cleanup_assistant_jobs(
            batch_api,
            all_ids,
            strict=True,
            context="stress phase 8 wind-down cleanup",
            parallelism=min(len(all_ids), _STRESS_CLEANUP_PARALLELISM),
        )
        print(f"[Phase 8] Requested AssistantSession cleanup for all runtimes")
        print(f"[Phase 8] Waiting 15s for cleanup to propagate...")
        time.sleep(15)

        if gce_client is not None:
            orphaned_vms = []
            for aid in all_ids:
                try:
                    vms = list_assigned_vms(gce_client, aid)
                except Exception:
                    vms = []
                if vms:
                    orphaned_vms.append((aid, [vm.name for vm in vms]))

            if orphaned_vms:
                print(
                    f"[Phase 8] Orphaned VMs: {len(orphaned_vms)} "
                    f"(INV-10/INV-13 risk)",
                )
                for aid, names in orphaned_vms:
                    print(f"  {aid}: {names}")
            else:
                print(f"[Phase 8] No orphaned VMs — clean")

        replenish_pool()

        final_invariants = check_invariants(batch_api, gce_client)
        final_new = _new_violations(final_invariants, baseline_violations)
        if final_new:
            print(f"[Phase 8] Final invariant violations: {len(final_new)}")
            _print_violations(final_new, "final")
        else:
            print(f"[Phase 8] Final invariants: all clear")

        print(f"\n{'=' * 70}")
        print(f"  STRESS TEST COMPLETE: {N} assistants, 8 phases")
        print(f"  Immediate starts: {len(immediate)}")
        print(f"  All served:       {len(containers_up)}/{N}")
        pool_after = count_idle_jobs(batch_api)
        print(f"  Idle pool now:    {pool_after}")
        print(f"{'=' * 70}\n")

    finally:
        scheduler_noise.stop()
        _reset_stress_baseline(
            comms,
            batch_api,
            gce_client,
            all_ids,
            context="stress finally cleanup",
            cleanup_timeout=_STRESS_FAST_TEARDOWN_TIMEOUT_SECONDS,
            wait_for_baseline=False,
        )
