from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from threading import Lock
from typing import Any, Callable

from communication.infra.assistant_sessions import (
    DESIRED_STATE_STOPPED,
    SIGNAL_VM_GUEST_HEALTH,
    SIGNAL_VM_RELEASE_REQUEST,
    assistant_session_desired_state,
    binding_id as binding_id_from_status,
    build_binding_signal,
    emit_observability_event,
    get_assistant_session,
    persist_binding_vm_assignment_result,
    read_bootstrap_secret,
    record_assistant_session_signal,
    session_binding,
)
from communication.infra.observability import (
    bind_causal_context,
    build_causal_context,
    causal_log_fields,
    current_causal_context,
)
from communication.infra.gcp_region_catalog import placement_from_ref
from communication.infra.vm_helpers import (
    AssistantDiskInUseError,
    assign_pool_vm,
    prepare_assistant_cross_region_migration,
    probe_vm_agent_service,
    release_pool_vm,
    replenish_pool,
    vm_placement_scope,
)

logger = logging.getLogger(__name__)

_DEFAULT_WORKER_CONCURRENCY = int(
    os.environ.get("ASSISTANT_SESSION_WORKER_CONCURRENCY", "8"),
)


class _TaskRuntime:
    """Run slow side effects in the background with per-binding de-duplication."""

    def __init__(self, max_workers: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="session-worker",
        )
        self._lock = Lock()
        self._inflight: set[tuple[str, str, str]] = set()

    def submit(
        self,
        *,
        task_type: str,
        task_assistant_id: str,
        task_binding_id: str,
        fn: Callable[..., Any],
        **kwargs: Any,
    ) -> bool:
        key = (task_type, task_assistant_id, task_binding_id)
        worker_context = build_causal_context(
            caller=f"worker.{task_type}",
            parent=current_causal_context(),
        )
        with self._lock:
            if key in self._inflight:
                emit_observability_event(
                    "controller.worker.deduplicated",
                    task_type=task_type,
                    assistant_id=task_assistant_id,
                    binding_id=task_binding_id,
                    **causal_log_fields(worker_context),
                )
                return False
            self._inflight.add(key)
            inflight_count = len(self._inflight)
        emit_observability_event(
            "controller.worker.enqueued",
            task_type=task_type,
            assistant_id=task_assistant_id,
            binding_id=task_binding_id,
            inflight_count=inflight_count,
            **causal_log_fields(worker_context),
        )
        future = self._executor.submit(
            self._run_task,
            key,
            task_type,
            worker_context,
            fn,
            kwargs,
        )
        future.add_done_callback(
            lambda _future: self._finish(key, task_type, worker_context, _future),
        )
        return True

    def _run_task(
        self,
        key: tuple[str, str, str],
        task_type: str,
        worker_context: dict[str, Any],
        fn: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> Any:
        with bind_causal_context(worker_context):
            emit_observability_event(
                "controller.worker.started",
                task_type=task_type,
                assistant_id=key[1],
                binding_id=key[2],
            )
            return fn(**kwargs)

    def _finish(
        self,
        key: tuple[str, str, str],
        task_type: str,
        worker_context: dict[str, Any],
        future,
    ) -> None:
        with self._lock:
            self._inflight.discard(key)
            inflight_count = len(self._inflight)
        exception = future.exception()
        if exception is not None:
            emit_observability_event(
                "controller.worker.failed",
                task_type=task_type,
                assistant_id=key[1],
                binding_id=key[2],
                inflight_count=inflight_count,
                error_type=type(exception).__name__,
                error=str(exception),
                **causal_log_fields(worker_context),
            )
        emit_observability_event(
            "controller.worker.completed",
            task_type=task_type,
            assistant_id=key[1],
            binding_id=key[2],
            inflight_count=inflight_count,
            **causal_log_fields(worker_context),
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"inflight": len(self._inflight)}


_runtime: _TaskRuntime | None = None
_runtime_lock = Lock()


def get_worker_runtime() -> _TaskRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = _TaskRuntime(_DEFAULT_WORKER_CONCURRENCY)
    return _runtime


def worker_runtime_stats() -> dict[str, int]:
    return get_worker_runtime().stats()


def _binding_is_current(
    custom_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
) -> bool:
    session = get_assistant_session(custom_api, namespace, assistant_id)
    if not session:
        return False
    if assistant_session_desired_state(session) == DESIRED_STATE_STOPPED:
        return False
    return binding_id_from_status(session_binding(session)) == binding_id


def schedule_vm_assignment(
    *,
    custom_api,
    core_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    attempt_id: str,
    secret_name: str,
    vm_type: str,
) -> bool:
    """Queue a VM assignment attempt for the current binding."""

    return get_worker_runtime().submit(
        task_type="vm_assignment",
        task_assistant_id=assistant_id,
        task_binding_id=binding_id,
        fn=_run_vm_assignment,
        custom_api=custom_api,
        core_api=core_api,
        namespace=namespace,
        assistant_id=assistant_id,
        binding_id=binding_id,
        attempt_id=attempt_id,
        secret_name=secret_name,
        vm_type=vm_type,
    )


def _run_vm_assignment(
    *,
    custom_api,
    core_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    attempt_id: str,
    secret_name: str,
    vm_type: str,
) -> None:
    if not _binding_is_current(custom_api, namespace, assistant_id, binding_id):
        emit_observability_event(
            "controller.worker.vm_assignment.skipped",
            assistant_id=assistant_id,
            binding_id=binding_id,
            reason="binding_not_current",
        )
        return

    session = get_assistant_session(custom_api, namespace, assistant_id) or {}
    placement_payload = (
        (session.get("spec") or {}).get("desktop", {}).get("placement")
    )
    placement = placement_from_ref(
        placement_payload if isinstance(placement_payload, dict) else None
    )
    source_vm_ref = session_binding(session).get("vmRef") or {}
    source_placement = placement_from_ref(
        source_vm_ref if isinstance(source_vm_ref, dict) else None
    )
    migration = None
    if (
        placement is not None
        and source_placement is not None
        and source_placement.region != placement.region
    ):
        migration = {
            "id": binding_id,
            "source": {
                "poolLocation": source_placement.location.id,
                "region": source_placement.region,
                "zone": source_placement.zone,
            },
        }
    startup_payload = read_bootstrap_secret(core_api, namespace, secret_name)
    api_key = str(startup_payload.get("api_key", "") or "")
    try:
        if migration is not None:
            # Preparation is replay-safe and leaves the source disk/IP intact.
            # The target address is attached only after the target VM reports
            # readiness below.
            prepare_assistant_cross_region_migration(
                assistant_id=assistant_id,
                migration_id=migration["id"],
                source=source_placement,
                target=placement,
            )
        result = assign_pool_vm(
            assistant_id=assistant_id,
            binding_id=binding_id,
            unify_apikey=api_key,
            vm_type=vm_type,
            placement=placement,
            attach_static_ip=migration is None,
        )
        persisted = persist_binding_vm_assignment_result(
            custom_api,
            namespace,
            assistant_id,
            target_binding_id=binding_id,
            attempt_id=attempt_id,
            state="assigned",
            vm_ref={
                "name": result["vm_name"],
                "hostname": result["hostname"],
                "vmType": vm_type,
                "poolLocation": result["pool_location"],
                "region": result["region"],
                "zone": result["zone"],
                **({"regionalMigration": migration} if migration is not None else {}),
            },
            source="worker.vm_assignment",
        )
        if persisted:
            return
        emit_observability_event(
            "controller.worker.vm_assignment.release_stale_result",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=result["vm_name"],
        )
        try:
            release_pool_vm(
                assistant_id,
                binding_id,
                vm_name=result["vm_name"],
                placement=placement,
            )
        except Exception:
            logger.exception(
                "Failed releasing stale VM assignment for %s",
                assistant_id,
            )
        return
    except ValueError as exc:
        with vm_placement_scope(placement):
            replenish_pool(vm_type)
        persist_binding_vm_assignment_result(
            custom_api,
            namespace,
            assistant_id,
            target_binding_id=binding_id,
            attempt_id=attempt_id,
            state="capacity",
            message=str(exc),
            source="worker.vm_assignment",
        )
        return
    except AssistantDiskInUseError as exc:
        persist_binding_vm_assignment_result(
            custom_api,
            namespace,
            assistant_id,
            target_binding_id=binding_id,
            attempt_id=attempt_id,
            state="waiting_release",
            message=str(exc),
            source="worker.vm_assignment",
        )
        return
    except Exception as exc:  # pragma: no cover - worker safety net
        logger.exception("Background VM assignment failed for %s", assistant_id)
        persist_binding_vm_assignment_result(
            custom_api,
            namespace,
            assistant_id,
            target_binding_id=binding_id,
            attempt_id=attempt_id,
            state="error",
            message=f"{type(exc).__name__}: {exc}",
            source="worker.vm_assignment",
        )


def schedule_guest_health_probe(
    *,
    custom_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    vm_ref: dict[str, Any],
) -> bool:
    """Queue a guest liveness probe for a binding that already signaled ready."""

    return get_worker_runtime().submit(
        task_type="guest_probe",
        task_assistant_id=assistant_id,
        task_binding_id=binding_id,
        fn=_run_guest_health_probe,
        custom_api=custom_api,
        namespace=namespace,
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_ref=vm_ref,
    )


def _run_guest_health_probe(
    *,
    custom_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    vm_ref: dict[str, Any],
) -> None:
    if not _binding_is_current(custom_api, namespace, assistant_id, binding_id):
        emit_observability_event(
            "controller.worker.guest_probe.skipped",
            assistant_id=assistant_id,
            binding_id=binding_id,
            reason="binding_not_current",
        )
        return

    hostname = str(vm_ref.get("hostname", "") or "")
    alive = bool(hostname) and probe_vm_agent_service(hostname, timeout=3.0)
    payload = build_binding_signal(
        binding_id=binding_id,
        state="ready" if alive else "failed",
        message="" if alive else "Desktop liveness probe failed",
        vmRef=vm_ref,
    )
    record_assistant_session_signal(
        custom_api,
        namespace,
        assistant_id,
        signal_name=SIGNAL_VM_GUEST_HEALTH,
        payload=payload,
        source="worker.guest_probe",
    )


def schedule_vm_release_request(
    *,
    custom_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    vm_name: str,
    release_generation: int,
) -> bool:
    """Queue a release request for the current binding VM."""

    return get_worker_runtime().submit(
        task_type="vm_release_request",
        task_assistant_id=assistant_id,
        task_binding_id=binding_id,
        fn=_run_vm_release_request,
        custom_api=custom_api,
        namespace=namespace,
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_name=vm_name,
        release_generation=release_generation,
    )


def _run_vm_release_request(
    *,
    custom_api,
    namespace: str,
    assistant_id: str,
    binding_id: str,
    vm_name: str,
    release_generation: int,
) -> None:
    session = get_assistant_session(custom_api, namespace, assistant_id)
    if not session or binding_id_from_status(session_binding(session)) != binding_id:
        emit_observability_event(
            "controller.worker.vm_release_request.skipped",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            reason="binding_not_current",
        )
        return

    current_vm_ref = (session_binding(session).get("vmRef") or {})
    placement = placement_from_ref(
        current_vm_ref if isinstance(current_vm_ref, dict) else None
    )
    try:
        result = release_pool_vm(
            assistant_id,
            binding_id,
            vm_name=vm_name,
            release_generation=release_generation,
            placement=placement,
        )
        if result.get("retired"):
            with vm_placement_scope(placement):
                replenish_pool(str(result.get("vm_type", "ubuntu") or "ubuntu"))
        if result.get("retired"):
            state = "retired"
        elif result.get("released") or result.get("pool_role") == "releasing":
            state = "requested"
        else:
            state = "skipped"
        payload = build_binding_signal(
            binding_id=binding_id,
            state=state,
            vmName=vm_name,
            poolRole=result.get("pool_role"),
            releaseGeneration=result.get("release_generation") or release_generation,
            message=str(result.get("reason", "") or ""),
        )
    except Exception as exc:  # pragma: no cover - worker safety net
        logger.exception("Background VM release request failed for %s", assistant_id)
        payload = build_binding_signal(
            binding_id=binding_id,
            state="error",
            vmName=vm_name,
            releaseGeneration=release_generation,
            message=f"{type(exc).__name__}: {exc}",
        )
    record_assistant_session_signal(
        custom_api,
        namespace,
        assistant_id,
        signal_name=SIGNAL_VM_RELEASE_REQUEST,
        payload=payload,
        source="worker.vm_release_request",
    )
