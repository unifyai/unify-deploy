"""
Production traffic stress test.

Simulates a product launch: many users hitting the system simultaneously
with diverse, overlapping traffic patterns.  Unlike the existing
integration tests (which are sequential: start 1 assistant, verify,
clean up, start the next), everything here happens at once.

Driven by the ``test_assistants`` session fixture — set
``TEST_CREATE_ASSISTANT_COUNT`` to control scale:

    # Quick (~3 min)
    TEST_CREATE_ASSISTANT_COUNT=3 pytest tests/infra/integration/test_stress.py -v -s

    # Full stress (~12 min)
    TEST_CREATE_ASSISTANT_COUNT=20 pytest tests/infra/integration/test_stress.py -v -s
"""

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    check_invariants,
    cleanup_assistant_jobs,
    count_idle_jobs,
    list_assigned_vms,
    list_jobs_with_assistant_id,
    poll_until,
    probe_vm_agent_service,
    replenish_staging_pool,
    send_test_meet,
    send_test_message,
    send_test_system_event,
    wait_for_container_running,
)

pytestmark = [pytest.mark.staging]


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


def _get_hostname(assistant_id):
    """Derive the VM hostname for a staging assistant."""
    return f"unity-assistant-{assistant_id}-staging.vm.unify.ai"


# ---------------------------------------------------------------------------
# The stress test
# ---------------------------------------------------------------------------


def test_production_traffic_stress(
    test_assistants,
    comms,
    batch_api,
    gce_client,
    poll,
):
    """Simulate a product launch: N simultaneous users with diverse traffic.

    Five overlapping phases exercise the system under realistic concurrent
    load.  The idle pool is intentionally NOT pre-scaled so pool exhaustion,
    overflow queuing, and replenishment are all exercised.
    """
    assistants = test_assistants
    if len(assistants) < 2:
        pytest.skip(
            "Need at least 2 test assistants " "(set TEST_CREATE_ASSISTANT_COUNT >= 2)",
        )

    N = len(assistants)
    all_ids = [a["assistant_id"] for a in assistants]

    print(f"\n{'=' * 70}")
    print(f"  STRESS TEST: {N} assistants")
    print(f"{'=' * 70}")

    idle_before = count_idle_jobs(batch_api)
    print(f"[Setup] Idle pool: {idle_before} containers")

    baseline_violations = check_invariants(batch_api, gce_client)
    if baseline_violations:
        print(f"[Setup] Pre-existing invariant violations: {len(baseline_violations)}")
        _print_violations(baseline_violations, "baseline")

    try:
        # ==================================================================
        # PHASE 1: Thundering Herd — start all N assistants at once
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 1] Thundering herd: {N} concurrent /infra/job/start requests")
        print(f"          Pool has {idle_before} idle containers — expecting overflow")
        print(f"{'—' * 70}")

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=N) as pool:
            futures = {
                pool.submit(_start_job_tolerant, comms, a): a["assistant_id"]
                for a in assistants
            }
            results = {}
            for f in as_completed(futures):
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

        # Replenish + reconcile for overflow assistants
        if queued:
            print(f"[Phase 1] Replenishing pool for {len(queued)} queued startups...")
            overflow_count = len(queued)
            for _ in range(overflow_count):
                replenish_staging_pool()

            try:
                poll_until(
                    lambda: count_idle_jobs(batch_api) >= min(overflow_count, 2),
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

        msg_count = 0
        meet_count = 0
        event_count = 0
        adapter_errors = []
        for f in as_completed(traffic_futures):
            try:
                resp = f.result()
                if resp.status_code >= 500:
                    adapter_errors.append(resp.status_code)
            except Exception:
                pass

        msg_count = N * 2
        meet_count = N
        event_count = N
        elapsed_p2 = time.monotonic() - t0

        print(
            f"[Phase 2] Sent {msg_count} messages, {meet_count} meets, "
            f"{event_count} events ({elapsed_p2:.1f}s)",
        )
        if adapter_errors:
            print(f"[Phase 2] Adapter 5xx errors: {len(adapter_errors)}")
        print(f"[Phase 2] Pool: {count_idle_jobs(batch_api)} idle containers")

        assert (
            not adapter_errors
        ), f"{len(adapter_errors)} adapter 5xx errors during traffic firehose"

        # ==================================================================
        # PHASE 3: Steady State Verification
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 3] Waiting for all {N} containers to be running...")
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
                print(f"  {aid}: running ({containers_up[aid]})")
            except TimeoutError:
                containers_failed[aid] = "timeout"
                print(f"  {aid}: TIMEOUT — no container after 300s")

        elapsed_p3_wait = time.monotonic() - t0
        print(
            f"\n[Phase 3] Containers: {len(containers_up)}/{N} running, "
            f"{len(containers_failed)} failed ({elapsed_p3_wait:.1f}s)",
        )

        assert (
            len(containers_failed) == 0
        ), f"{len(containers_failed)} assistants never got a container: " + ", ".join(
            containers_failed.keys()
        )

        # Verify one container per assistant (INV-1)
        for aid in all_ids:
            jobs = list_jobs_with_assistant_id(batch_api, aid)
            assert len(jobs) <= 1, (
                f"INV-1: assistant {aid} has {len(jobs)} containers "
                f"({[j.metadata.name for j in jobs]})"
            )

        # Verify VM assignments (INV-9) and auth (INV-11)
        if gce_client is not None:
            vm_assigned = 0
            vm_auth_ok = 0
            vm_auth_fail = 0
            for a in assistants:
                aid = a["assistant_id"]
                vms = list_assigned_vms(gce_client, aid)
                if vms:
                    vm_assigned += 1
                    hostname = _get_hostname(aid)
                    resp = probe_vm_agent_service(hostname, a["api_key"])
                    if resp and resp.status_code == 200:
                        vm_auth_ok += 1
                    else:
                        vm_auth_fail += 1
                        status = resp.status_code if resp else "no response"
                        print(f"  VM auth fail: {aid} ({hostname}) — {status}")

            print(
                f"[Phase 3] VMs: {vm_assigned}/{N} assigned, "
                f"{vm_auth_ok} auth OK, {vm_auth_fail} auth FAIL",
            )

        p3_invariants = check_invariants(batch_api, gce_client)
        p3_new = _new_violations(p3_invariants, baseline_violations)
        if p3_new:
            print(f"[Phase 3] Invariant violations: {len(p3_new)}")
            _print_violations(p3_new)
        else:
            print(f"[Phase 3] Invariants: all clear")

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
                time.sleep(30)

        # ==================================================================
        # PHASE 5: Wind-down and Cleanup
        # ==================================================================
        print(f"\n{'—' * 70}")
        print(f"[Phase 5] Cleaning up {N} assistants...")
        print(f"{'—' * 70}")

        cleanup_assistant_jobs(batch_api, all_ids)
        print(f"[Phase 5] Jobs deleted, waiting 30s for watcher processing...")
        time.sleep(30)

        if gce_client is not None:
            orphaned_vms = []
            for aid in all_ids:
                vms = list_assigned_vms(gce_client, aid)
                if vms:
                    orphaned_vms.append((aid, [vm.name for vm in vms]))

            if orphaned_vms:
                print(
                    f"[Phase 5] Orphaned VMs: {len(orphaned_vms)} "
                    f"(INV-10/INV-13 risk)",
                )
                for aid, names in orphaned_vms:
                    print(f"  {aid}: {names}")
            else:
                print(f"[Phase 5] No orphaned VMs — clean")

        replenish_staging_pool()

        final_invariants = check_invariants(batch_api, gce_client)
        final_new = _new_violations(final_invariants, baseline_violations)
        if final_new:
            print(f"[Phase 5] Final invariant violations: {len(final_new)}")
            _print_violations(final_new, "final")
        else:
            print(f"[Phase 5] Final invariants: all clear")

        print(f"\n{'=' * 70}")
        print(f"  STRESS TEST COMPLETE: {N} assistants")
        print(f"  Immediate starts: {len(immediate)}")
        print(f"  Queued starts:    {len(queued)}")
        print(f"  All served:       {len(containers_up)}/{N}")
        pool_after = count_idle_jobs(batch_api)
        print(f"  Idle pool now:    {pool_after}")
        print(f"{'=' * 70}\n")

    finally:
        cleanup_assistant_jobs(batch_api, all_ids)
        replenish_staging_pool()
