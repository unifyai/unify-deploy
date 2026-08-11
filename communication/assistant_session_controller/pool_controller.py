from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from common.settings import SETTINGS
from communication.infra.assistant_sessions import (
    assistant_session_desired_state,
    binding_job_ref,
    binding_vm_ref,
    emit_observability_event,
    get_custom_objects_api,
    session_binding,
    session_desktop_mode,
    session_desktop_required,
)
from communication.infra.idle_job_pool import schedule_idle_job_pool_replenishment
from communication.infra.vm_config import SUPPORTED_POOL_VM_TYPES
from communication.infra.gcp_region_catalog import VmPlacement, placement_from_ref
from communication.infra.vm_helpers import (
    replenish_pool,
    sync_assistant_static_ip_attachment,
    trim_pool,
    vm_placement_scope,
)

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", SETTINGS.default_namespace)
POOL_CONTROLLER_INTERVAL_SECONDS = float(
    os.environ.get("POOL_CONTROLLER_INTERVAL_SECONDS", "10"),
)
MIN_IDLE_JOBS = int(os.environ.get("UNITY_MIN_IDLE_JOBS", "3"))
ASSISTANT_IP_SYNC_INTERVAL_SECONDS = float(
    os.environ.get("UNITY_ASSISTANT_IP_SYNC_INTERVAL_SECONDS", "300"),
)
_IMAGE_HASH_LABEL = "unity-image-hash"
_last_assistant_ip_sync_at = 0.0


def _get_current_image_hash() -> str | None:
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    try:
        response = requests.get(
            f"{SETTINGS.comms_url}/infra/image",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        commit_hash = response.json().get("commit_hash")
        return commit_hash.strip() if commit_hash else None
    except Exception:
        logger.exception("Failed to fetch current image hash from comms")
        return None


def _count_idle_jobs_by_hash(current_hash: str) -> tuple[int, int]:
    """Return ``(matching, total)`` idle Unity jobs for the current environment."""

    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    response = requests.get(
        f"{SETTINGS.comms_url}/infra/jobs",
        params={
            "label_selector": "app=unity,unity-status=idle",
            "hours": 24,
        },
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    jobs = response.json().get("jobs", [])
    total = 0
    matching = 0
    for job in jobs:
        job_name = job.get("job_name", "")
        if SETTINGS.env_suffix:
            if not job_name.endswith(SETTINGS.env_suffix):
                continue
        elif job_name.endswith("-staging"):
            continue
        total += 1
        if job.get("labels", {}).get(_IMAGE_HASH_LABEL) == current_hash:
            matching += 1
    return matching, total


def _unity_job_replenish_extra_demand(pending_jobs: int) -> int:
    current_hash = _get_current_image_hash()
    if not current_hash:
        return pending_jobs

    matching, _total = _count_idle_jobs_by_hash(current_hash)
    hash_deficit = max(0, MIN_IDLE_JOBS - matching)
    return max(pending_jobs, hash_deficit)


def _list_sessions(custom_api, namespace: str) -> list[dict[str, Any]]:
    response = custom_api.list_namespaced_custom_object(
        group=SETTINGS.assistant_session_group,
        version=SETTINGS.assistant_session_version,
        namespace=namespace,
        plural=SETTINGS.assistant_session_plural,
    )
    items = response.get("items") if isinstance(response, dict) else []
    return items if isinstance(items, list) else []


def pending_vm_demand(sessions: list[dict[str, Any]]) -> dict[str, int]:
    """Return queue depth per VM type for sessions waiting on VM capacity."""

    demand: dict[str, int] = {vm_type: 0 for vm_type in SUPPORTED_POOL_VM_TYPES}
    for session in sessions:
        if assistant_session_desired_state(session) != "Running":
            continue
        if not session_desktop_required(session):
            continue
        if binding_vm_ref(session_binding(session)):
            continue
        phase = str(((session.get("status") or {}).get("phase")) or "")
        if phase != "PendingVM":
            continue
        vm_type = session_desktop_mode(session) or "ubuntu"
        if vm_type not in demand:
            vm_type = "ubuntu"
        demand[vm_type] += 1
    return demand


def pending_vm_demand_by_pool(
    sessions: list[dict[str, Any]],
) -> tuple[dict[tuple[str, VmPlacement | None], int], list[dict[str, str]]]:
    """Return pending desktop demand per VM type and physical pool placement."""

    demand: dict[tuple[str, VmPlacement | None], int] = {}
    invalid: list[dict[str, str]] = []
    for session in sessions:
        if assistant_session_desired_state(session) != "Running":
            continue
        if not session_desktop_required(session):
            continue
        if binding_vm_ref(session_binding(session)):
            continue
        phase = str(((session.get("status") or {}).get("phase")) or "")
        if phase != "PendingVM":
            continue
        vm_type = session_desktop_mode(session) or "ubuntu"
        if vm_type not in SUPPORTED_POOL_VM_TYPES:
            vm_type = "ubuntu"
        spec = session.get("spec") or {}
        placement_payload = (spec.get("desktop") or {}).get("placement")
        try:
            placement = placement_from_ref(
                placement_payload if isinstance(placement_payload, dict) else None,
            )
        except ValueError as exc:
            invalid.append(
                {
                    "assistant_id": str(spec.get("assistantId") or ""),
                    "error": str(exc),
                },
            )
            continue
        key = (vm_type, placement)
        demand[key] = demand.get(key, 0) + 1
    return demand, invalid


def _pool_result_key(vm_type: str, placement: VmPlacement | None) -> str:
    if placement is None:
        return f"{vm_type}:legacy"
    return f"{vm_type}:{placement.location.id}:{placement.zone}"


def _pool_zone_scopes(
    demand: dict[tuple[str, VmPlacement | None], int],
) -> tuple[
    dict[tuple[str, str], VmPlacement | None],
    dict[tuple[str, str], int],
]:
    """Collapse pending demand onto one scope per physical ``(vm_type, zone)`` pool.

    A placement naming the default zone and the legacy ``None`` placement address
    the same VMs. Iterating them as separate scopes gave each its own demand
    number, so the legacy scope — which saw zero pending — trimmed the spare VM
    the placement scope had just replenished for a queued session, and the pool
    flapped that VM between stopped and started every couple of minutes.

    Returns ``(scopes, zone_demand)``: one representative placement per physical
    pool (preferring an explicit placement over the legacy default, so work runs
    against a fully-qualified location) and the summed demand for it.
    """

    scopes: dict[tuple[str, str], VmPlacement | None] = {}
    zone_demand: dict[tuple[str, str], int] = {}
    for (vm_type, placement), count in demand.items():
        key = (vm_type, placement.zone if placement else SETTINGS.vm_zone)
        zone_demand[key] = zone_demand.get(key, 0) + count
        if scopes.get(key) is None:
            scopes[key] = placement
    for vm_type in SUPPORTED_POOL_VM_TYPES:
        scopes.setdefault((vm_type, SETTINGS.vm_zone), None)
    return scopes, zone_demand


def pending_job_demand(sessions: list[dict[str, Any]]) -> int:
    """Return the number of sessions blocked on idle Unity container capacity."""

    pending = 0
    for session in sessions:
        if assistant_session_desired_state(session) != "Running":
            continue
        if binding_job_ref(session_binding(session)):
            continue
        phase = str(((session.get("status") or {}).get("phase")) or "")
        if phase != "PendingJob":
            continue
        pending += 1
    return pending


def sync_active_assistant_ip_reports(sessions: list[dict[str, Any]]) -> int:
    """Backfill Orchestra network identities for active desktop bindings."""

    synced = 0
    for session in sessions:
        if not session_desktop_required(session):
            continue
        spec = session.get("spec") or {}
        assistant_id = str(spec.get("assistantId") or "")
        vm_ref = binding_vm_ref(session_binding(session))
        if not assistant_id or not isinstance(vm_ref, dict):
            continue
        try:
            placement = placement_from_ref(vm_ref)
            if sync_assistant_static_ip_attachment(assistant_id, placement):
                synced += 1
        except Exception:
            logger.exception(
                "Failed syncing assistant network identity for %s",
                assistant_id,
            )
    return synced


def reconcile_pool_once(custom_api, namespace: str = WATCH_NAMESPACE) -> dict[str, Any]:
    """Run one pool-capacity reconciliation cycle."""

    sessions = _list_sessions(custom_api, namespace)
    pending_jobs = pending_job_demand(sessions)
    demand, invalid_demand = pending_vm_demand_by_pool(sessions)
    results: dict[str, Any] = {}
    global _last_assistant_ip_sync_at
    if (
        time.monotonic() - _last_assistant_ip_sync_at
        >= ASSISTANT_IP_SYNC_INTERVAL_SECONDS
    ):
        synced = sync_active_assistant_ip_reports(sessions)
        _last_assistant_ip_sync_at = time.monotonic()
        results["assistant_ip_sync"] = {"synced": synced}
    try:
        job_extra_demand = _unity_job_replenish_extra_demand(pending_jobs)
        replenish_scheduled = (
            schedule_idle_job_pool_replenishment(
                extra_demand=job_extra_demand,
                source="controller.pool_reconcile",
            )
            if job_extra_demand > 0
            else False
        )
        results["unity_jobs"] = {
            "pending_sessions": pending_jobs,
            "replenish_extra_demand": job_extra_demand,
            "replenish_scheduled": replenish_scheduled,
        }
        emit_observability_event(
            "controller.job_pool.reconcile",
            namespace=namespace,
            pending_sessions=pending_jobs,
            replenish_extra_demand=job_extra_demand,
            replenish_scheduled=replenish_scheduled,
        )
    except Exception as exc:  # pragma: no cover - safety net for live loop
        logger.exception("Pool controller reconcile failed for unity jobs")
        results["unity_jobs"] = {
            "pending_sessions": pending_jobs,
            "error": f"{type(exc).__name__}: {exc}",
        }
        emit_observability_event(
            "controller.job_pool.reconcile_failed",
            namespace=namespace,
            pending_sessions=pending_jobs,
            error=f"{type(exc).__name__}: {exc}",
        )
    results["vm_pools"] = {}
    pool_scopes, zone_demand = _pool_zone_scopes(demand)
    aggregate_demand = pending_vm_demand(sessions)
    for (vm_type, zone), placement in sorted(
        pool_scopes.items(),
        key=lambda item: _pool_result_key(item[0][0], item[1]),
    ):
        pending = zone_demand.get((vm_type, zone), 0)
        pool_key = _pool_result_key(vm_type, placement)
        try:
            with vm_placement_scope(placement):
                replenish_result = replenish_pool(vm_type, extra_demand=pending)
                # Trim every cycle now that it knows the queue depth: gating it
                # on "no pending sessions" was a stand-in for demand-awareness
                # and left excess idle capacity running whenever anything queued.
                trim_result = trim_pool(vm_type, extra_demand=pending)
            pool_result = {
                "pending_sessions": pending,
                "replenish": replenish_result,
                "trim": trim_result,
            }
            results["vm_pools"][pool_key] = pool_result
            results[vm_type] = {
                "pending_sessions": aggregate_demand.get(vm_type, 0),
            }
            emit_observability_event(
                "controller.pool.reconcile",
                namespace=namespace,
                vm_type=vm_type,
                pending_sessions=pending,
                pool_location=placement.location.id if placement else None,
                region=placement.region if placement else SETTINGS.vm_region,
                zone=placement.zone if placement else SETTINGS.vm_zone,
                replenish_result=replenish_result,
                trim_result=trim_result,
            )
        except Exception as exc:  # pragma: no cover - safety net for live loop
            logger.exception("Pool controller reconcile failed for %s", pool_key)
            results["vm_pools"][pool_key] = {
                "pending_sessions": pending,
                "error": f"{type(exc).__name__}: {exc}",
            }
            emit_observability_event(
                "controller.pool.reconcile_failed",
                namespace=namespace,
                vm_type=vm_type,
                pending_sessions=pending,
                pool_location=placement.location.id if placement else None,
                region=placement.region if placement else SETTINGS.vm_region,
                zone=placement.zone if placement else SETTINGS.vm_zone,
                error=f"{type(exc).__name__}: {exc}",
            )
    for invalid in invalid_demand:
        emit_observability_event(
            "controller.pool.reconcile_invalid_placement",
            namespace=namespace,
            **invalid,
        )
    if invalid_demand:
        results["invalid_vm_placements"] = invalid_demand
    return results


def run_forever() -> None:
    """Continuously reconcile generic pool supply for sessions."""

    custom_api = get_custom_objects_api()
    if custom_api is None:
        raise RuntimeError("Failed to initialize AssistantSession API client")

    while True:
        reconcile_pool_once(custom_api, WATCH_NAMESPACE)
        time.sleep(POOL_CONTROLLER_INTERVAL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_forever()


if __name__ == "__main__":
    main()
