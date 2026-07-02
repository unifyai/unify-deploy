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
from communication.infra.vm_helpers import replenish_pool, trim_pool

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", SETTINGS.default_namespace)
POOL_CONTROLLER_INTERVAL_SECONDS = float(
    os.environ.get("POOL_CONTROLLER_INTERVAL_SECONDS", "10"),
)
MIN_IDLE_JOBS = int(os.environ.get("UNITY_MIN_IDLE_JOBS", "3"))
_IMAGE_HASH_LABEL = "unity-image-hash"


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


def reconcile_pool_once(custom_api, namespace: str = WATCH_NAMESPACE) -> dict[str, Any]:
    """Run one pool-capacity reconciliation cycle."""

    sessions = _list_sessions(custom_api, namespace)
    pending_jobs = pending_job_demand(sessions)
    demand = pending_vm_demand(sessions)
    results: dict[str, Any] = {}
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
    for vm_type in SUPPORTED_POOL_VM_TYPES:
        pending = demand.get(vm_type, 0)
        try:
            replenish_result = replenish_pool(vm_type, extra_demand=pending)
            trim_result = trim_pool(vm_type) if pending == 0 else None
            results[vm_type] = {
                "pending_sessions": pending,
                "replenish": replenish_result,
                "trim": trim_result,
            }
            emit_observability_event(
                "controller.pool.reconcile",
                namespace=namespace,
                vm_type=vm_type,
                pending_sessions=pending,
                replenish_result=replenish_result,
                trim_result=trim_result,
            )
        except Exception as exc:  # pragma: no cover - safety net for live loop
            logger.exception("Pool controller reconcile failed for %s", vm_type)
            results[vm_type] = {
                "pending_sessions": pending,
                "error": f"{type(exc).__name__}: {exc}",
            }
            emit_observability_event(
                "controller.pool.reconcile_failed",
                namespace=namespace,
                vm_type=vm_type,
                pending_sessions=pending,
                error=f"{type(exc).__name__}: {exc}",
            )
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
