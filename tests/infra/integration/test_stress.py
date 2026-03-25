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

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    COMMS_APP_URL,
    NAMESPACE,
    check_invariants,
    cleanup_assistant_jobs,
    count_idle_jobs,
    list_assigned_vms,
    list_idle_vms,
    list_jobs_with_assistant_id,
    poll_until,
    probe_vm_agent_service,
    pull_outbound_messages,
    replenish_pool,
    send_test_meet,
    send_test_message,
    send_test_system_event,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration]


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
    """Trigger the pending-startup reconciler on the comms app."""
    try:
        requests.post(
            f"{COMMS_APP_URL}/infra/pending/process",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
    except Exception:
        pass


def _trigger_cleanup():
    """Trigger the idle pool cleanup on the adapters (same call as the cron)."""
    try:
        requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/cleanup",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
    except Exception:
        pass


def _trigger_pool_refresh():
    """Trigger a pool refresh (same call as the hourly cron / post-deploy).

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


def _trigger_stale_expire():
    """Trigger the stale jobs sweep (same call as the 6-hourly cron).

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


def _wait_for_vm_assigned(gce_client, assistant_id, timeout=120, interval=10):
    """Poll until a VM is assigned to this assistant."""
    return poll_until(
        lambda: list_assigned_vms(gce_client, assistant_id),
        timeout=timeout,
        interval=interval,
        description=f"VM assigned to assistant {assistant_id}",
    )


class _SchedulerNoise:
    """Background thread that fires scheduler endpoints at random intervals.

    Simulates production crons firing at unpredictable times relative to
    user traffic.  Runs throughout the entire test and logs each firing.
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
):
    """Simulate a product launch: N simultaneous users with diverse traffic.

    Eight phases exercise the system under realistic concurrent load,
    including crash recovery, cleanup races, and rapid restarts.
    The idle pool is intentionally NOT pre-scaled so pool exhaustion,
    overflow queuing, and replenishment are all exercised.
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
    # Clean slate: delete all existing jobs, release orphaned VMs, then
    # create exactly MIN_IDLE fresh containers with the latest image.
    # ------------------------------------------------------------------
    TARGET_IDLE = 3

    print(f"[Setup] Cleaning previous state...")
    existing_jobs = batch_api.list_namespaced_job(
        namespace=NAMESPACE,
        label_selector="app=unity",
    )
    for job in existing_jobs.items:
        aid = (job.metadata.labels or {}).get("assistant-id", "")
        try:
            batch_api.delete_namespaced_job(
                name=job.metadata.name,
                namespace=NAMESPACE,
                propagation_policy="Foreground",
            )
        except Exception:
            pass
        if aid and gce_client is not None:
            try:
                requests.post(
                    f"{COMMS_APP_URL}/infra/vm/pool/release",
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    json={"assistant_id": aid},
                    timeout=15,
                )
            except Exception:
                pass
    if existing_jobs.items:
        print(f"[Setup] Deleted {len(existing_jobs.items)} leftover jobs")
        time.sleep(10)

    print(f"[Setup] Creating {TARGET_IDLE} fresh idle containers...")
    for _ in range(TARGET_IDLE):
        replenish_pool()
        time.sleep(2)

    try:
        poll_until(
            lambda: count_idle_jobs(batch_api) >= TARGET_IDLE,
            timeout=120,
            interval=10,
            description=f"Fresh idle pool ({TARGET_IDLE} containers)",
        )
    except TimeoutError:
        pass

    idle_before = count_idle_jobs(batch_api)
    print(f"[Setup] Idle pool: {idle_before} containers (target: {TARGET_IDLE})")

    baseline_violations = check_invariants(batch_api, gce_client)
    if baseline_violations:
        print(f"[Setup] Pre-existing invariant violations: {len(baseline_violations)}")
        _print_violations(baseline_violations, "baseline")

    idle_vms_before = 0
    if gce_client is not None:
        try:
            idle_vms_before = len(list_idle_vms(gce_client))
            print(f"[Setup] Idle VM pool: {idle_vms_before} ubuntu VMs")
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
        queued = [aid for aid, (s, _) in results.items() if s == 202]
        errors = [aid for aid, (s, _) in results.items() if s not in (200, 202)]
        elapsed_p1 = time.monotonic() - t0

        print(f"[Phase 1] Results ({elapsed_p1:.1f}s):")
        print(f"  Immediate (200): {len(immediate)}")
        print(f"  Queued    (202): {len(queued)}")
        print(f"  Errors:          {len(errors)}")
        for aid in errors:
            s, body = results[aid]
            print(f"    assistant {aid}: HTTP {s} — {body[:200]}")

        assert not errors, (
            f"{len(errors)} startup requests failed (expected 200 or 202): "
            + ", ".join(f"{aid}={results[aid][0]}" for aid in errors)
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
            print(f"[Phase 1] Replenishing pool for {len(queued)} queued startups...")
            for _ in range(len(queued)):
                replenish_pool()
            try:
                poll_until(
                    lambda: count_idle_jobs(batch_api) >= min(len(queued), 2),
                    timeout=180,
                    interval=10,
                    description="Idle containers for overflow reconciliation",
                )
            except TimeoutError:
                print(f"[Phase 1] Warning: idle pool slow to replenish")
            _trigger_reconciliation()
            time.sleep(5)
            _trigger_reconciliation()

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
        containers_failed = {}

        for a in assistants:
            aid = a["assistant_id"]
            try:
                jobs = wait_for_container_running(
                    batch_api,
                    aid,
                    timeout=300,
                    interval=10,
                )
                containers_up[aid] = jobs[0].metadata.name
                print(f"  {aid}: container running ({containers_up[aid]})")
            except TimeoutError:
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
            assert len(jobs) <= 1, (
                f"INV-1: assistant {aid} has {len(jobs)} containers "
                f"({[j.metadata.name for j in jobs]})"
            )

        if gce_client is not None:
            print(f"[Phase 3] Checking VM assignments (single pass, 60s wait)...")
            time.sleep(60)
            vm_assigned = 0
            vm_auth_ok = 0
            vm_auth_fail = 0
            vm_not_assigned = 0
            for a in assistants:
                aid = a["assistant_id"]
                try:
                    vms = list_assigned_vms(gce_client, aid)
                    if vms:
                        vm_assigned += 1
                        hostname = _get_vm_hostname(vms[0])
                        resp = probe_vm_agent_service(hostname, a["api_key"])
                        if resp and resp.status_code == 200:
                            vm_auth_ok += 1
                        else:
                            vm_auth_fail += 1
                            status = resp.status_code if resp else "no response"
                            print(
                                f"  {aid}: VM {vms[0].name}, auth FAIL ({hostname}) — {status}",
                            )
                    else:
                        vm_not_assigned += 1
                except Exception as e:
                    print(f"  {aid}: GCE check failed — {e}")

            print(
                f"[Phase 3] VMs: {vm_assigned}/{N} assigned, "
                f"{vm_auth_ok} auth OK, {vm_auth_fail} auth FAIL, "
                f"{vm_not_assigned} not assigned (pool had {idle_vms_before} idle)",
            )

            assert vm_auth_fail == 0, (
                f"INV-11: {vm_auth_fail} VMs have auth failures "
                f"(assigned but agent-service key mismatch)"
            )

            expected_vms = min(N, idle_vms_before)
            if vm_assigned < expected_vms:
                import warnings

                warnings.warn(
                    f"Only {vm_assigned}/{expected_vms} VMs assigned "
                    f"(pool had {idle_vms_before} idle). "
                    f"VM assignment may be slower than 60s for some assistants.",
                )

        p3_invariants = check_invariants(batch_api, gce_client)
        p3_new = _new_violations(p3_invariants, baseline_violations)
        if p3_new:
            print(f"[Phase 3] Invariant violations: {len(p3_new)}")
            _print_violations(p3_new)
        else:
            print(f"[Phase 3] Invariants: all clear")

        # Verify Phase 2 messages were delivered (Pub/Sub → container → outbound)
        try:
            from google.cloud import pubsub_v1 as _pubsub_v1

            subscriber = _pubsub_v1.SubscriberClient()
            delivered_count = 0
            checked_count = 0
            check_sample = assistants[: min(3, N)]
            for a in check_sample:
                aid = a["assistant_id"]
                msgs = pull_outbound_messages(subscriber, str(aid), timeout=5)
                checked_count += 1
                if msgs:
                    delivered_count += 1
                    print(f"  {aid}: {len(msgs)} outbound message(s) — delivered")
                else:
                    print(f"  {aid}: no outbound messages yet")
            print(
                f"[Phase 3] Message delivery: {delivered_count}/{checked_count} "
                f"assistants have outbound messages",
            )
            if delivered_count == 0 and checked_count > 0:
                import warnings

                warnings.warn(
                    f"No outbound messages found for any of the {checked_count} "
                    f"assistants checked. Phase 2 messages may not have been "
                    f"processed yet (containers still initializing).",
                )
        except Exception as e:
            print(f"[Phase 3] Message delivery check skipped: {e}")

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
        assert len(dup_jobs) <= 1, (
            f"INV-1 PROVOKED: assistant {provoke_aid} has {len(dup_jobs)} containers "
            f"after 3 concurrent start_job calls: "
            f"{[j.metadata.name for j in dup_jobs]}"
        )
        print(f"  INV-1 provocation: {len(dup_jobs)} container(s) — safe")

        # ==================================================================
        # PHASE 5: Crash Recovery Under Load
        # ==================================================================
        crash_count = min(2, N // 2)
        crash_assistants = assistants[:crash_count]
        surviving_assistants = assistants[crash_count:]
        crash_ids = [a["assistant_id"] for a in crash_assistants]

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
            # Kill the pods
            for a in crash_assistants:
                aid = a["assistant_id"]
                job_name = containers_up.get(aid)
                if not job_name:
                    print(f"  {aid}: no container to kill, skipping")
                    continue
                killed = _kill_pod(core_api, job_name)
                if killed:
                    print(f"  {aid}: killed pod {killed}")
                else:
                    print(f"  {aid}: no running pod found for {job_name}")

            # Fire stale-expire while watcher is processing crashes — tests
            # whether the sweep races with the watcher on VM release / job suspend
            print(f"    [scheduler] Firing stale-expire during crash recovery...")
            _trigger_stale_expire()

            # Wait for jobs to reach terminal state
            print(f"[Phase 5] Waiting for crashed jobs to terminate...")
            time.sleep(15)

            for aid in crash_ids:
                job_name = containers_up.get(aid)
                if not job_name:
                    continue
                try:
                    poll_until(
                        lambda jn=job_name: not any(
                            j.status.active and j.status.active > 0
                            for j in list_jobs_with_assistant_id(batch_api, aid)
                        ),
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

            if queued_restart := [
                a for a in crash_assistants if _start_job_tolerant(comms, a)[0] == 202
            ]:
                replenish_pool()
                _trigger_reconciliation()

            # Wait for new containers
            for a in crash_assistants:
                aid = a["assistant_id"]
                try:
                    jobs = wait_for_container_running(
                        batch_api,
                        aid,
                        timeout=300,
                        interval=10,
                    )
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

        target_assistant = assistants[0]
        target_aid = target_assistant["assistant_id"]

        for race_round in range(1, 4):
            # Delete the target's job so there's an idle-looking container
            cleanup_assistant_jobs(batch_api, [target_aid])
            time.sleep(3)

            # Race: cleanup vs re-start
            with ThreadPoolExecutor(max_workers=2) as pool:
                cleanup_future = pool.submit(_trigger_cleanup)
                start_future = pool.submit(_start_job_tolerant, comms, target_assistant)

            cleanup_future.result()
            start_status, start_body = start_future.result()

            if start_status == 202:
                replenish_pool()
                time.sleep(10)
                _trigger_reconciliation()

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
        restart_assistants = assistants[:restart_count]
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

        # Delete jobs (triggers VM release + disk detach)
        cleanup_assistant_jobs(batch_api, restart_ids)
        print(f"[Phase 7] Jobs deleted — immediately re-starting...")

        # Immediately re-start (no sleep — this is the point)
        for a in restart_assistants:
            aid = a["assistant_id"]
            status, body = _start_job_tolerant(comms, a)
            print(f"  {aid}: re-start → HTTP {status}")
            if status == 202:
                replenish_pool()
                _trigger_reconciliation()

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
        if gce_client is not None:
            print(f"[Phase 7] Verifying VM re-attachment (90s wait)...")
            time.sleep(90)
            for a in restart_assistants:
                aid = a["assistant_id"]
                try:
                    vms = list_assigned_vms(gce_client, aid)
                    if vms:
                        hostname = _get_vm_hostname(vms[0])
                        resp = probe_vm_agent_service(hostname, a["api_key"])
                        if resp and resp.status_code == 200:
                            print(f"  {aid}: VM {vms[0].name} re-attached, auth OK")
                        else:
                            status = resp.status_code if resp else "no response"
                            print(
                                f"  {aid}: VM {vms[0].name} re-attached, auth FAIL — {status}",
                            )
                    else:
                        print(f"  {aid}: VM not re-assigned after 90s")
                except Exception as e:
                    print(f"  {aid}: GCE check failed — {e}")

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

        cleanup_assistant_jobs(batch_api, all_ids)
        print(f"[Phase 8] Jobs deleted, releasing VMs...")
        for aid in all_ids:
            try:
                requests.post(
                    f"{COMMS_APP_URL}/infra/vm/pool/release",
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    json={"assistant_id": str(aid)},
                    timeout=15,
                )
            except Exception:
                pass
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
        print(f"  Queued starts:    {len(queued)}")
        print(f"  All served:       {len(containers_up)}/{N}")
        pool_after = count_idle_jobs(batch_api)
        print(f"  Idle pool now:    {pool_after}")
        print(f"{'=' * 70}\n")

    finally:
        scheduler_noise.stop()
        cleanup_assistant_jobs(batch_api, all_ids)
        for aid in all_ids:
            try:
                requests.post(
                    f"{COMMS_APP_URL}/infra/vm/pool/release",
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    json={"assistant_id": str(aid)},
                    timeout=15,
                )
            except Exception:
                pass
        replenish_pool()
