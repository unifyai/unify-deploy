from __future__ import annotations

import logging
import os
import time
from typing import Any

from common.settings import SETTINGS
from communication.infra.assistant_sessions import (
    assistant_session_desired_state,
    binding_vm_ref,
    emit_observability_event,
    get_custom_objects_api,
    session_binding,
    session_desktop_mode,
    session_desktop_required,
)
from communication.infra.vm_config import SUPPORTED_POOL_VM_TYPES
from communication.infra.vm_helpers import replenish_pool, trim_pool

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", SETTINGS.default_namespace)
POOL_CONTROLLER_INTERVAL_SECONDS = float(
    os.environ.get("POOL_CONTROLLER_INTERVAL_SECONDS", "10"),
)


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


def reconcile_pool_once(custom_api, namespace: str = WATCH_NAMESPACE) -> dict[str, Any]:
    """Run one pool-capacity reconciliation cycle."""

    sessions = _list_sessions(custom_api, namespace)
    demand = pending_vm_demand(sessions)
    results: dict[str, Any] = {}
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
    """Continuously reconcile generic pool supply for desktop sessions."""

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
