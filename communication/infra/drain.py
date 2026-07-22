"""Assistant drain / restart control plane.

Durable ``DrainIntent`` is stored in a namespaced ConfigMap so both live
sessions and headless offline Jobs can be gated without an AssistantSession.

Admission while draining:
- ``/infra/job/start`` refuses new wakes (except explicit restart completion)
- ``/infra/task-execution/offline-dispatch`` returns 503 so Cloud Tasks retries
- in-pod ``act`` refuses new work (unify polls ``GET /infra/assistants/{id}/restart``)

Graceful restart closes admission, waits until the busy-set is empty (or the
deadline), stops any live session, then clears the intent so deferred work can
run on the next wake with a fresh client bundle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from common.settings import SETTINGS
from communication.dependencies import authorize_admin_or_assistant
from communication.infra.runtime_clients import get_k8s_clients
from communication.infra.self_router import assistant_self_router

logger = logging.getLogger(__name__)

DRAIN_CONFIG_MAP = "unity-assistant-drain-intents"
DEFAULT_GRACEFUL_DEADLINE_SECONDS = 900
POLL_INTERVAL_SECONDS = 5.0

DrainMode = Literal["graceful", "force"]
DrainState = Literal[
    "pending",
    "draining",
    "stopping",
    "restarting",
    "done",
    "failed",
]

router = APIRouter()

# Background graceful waiters keyed by assistant_id (string).
_waiter_tasks: dict[str, asyncio.Task] = {}
_waiter_lock = asyncio.Lock()


class DrainIntent(BaseModel):
    assistant_id: str
    mode: DrainMode
    reason: str = "manual"
    target_revision: str | None = None
    requested_at: str
    deadline_at: str
    state: DrainState = "pending"
    block_admission: bool = True
    error: str | None = None


class RestartRequest(BaseModel):
    mode: DrainMode = "graceful"
    reason: str = "manual"
    target_revision: str | None = None
    deadline_seconds: int = Field(
        default=DEFAULT_GRACEFUL_DEADLINE_SECONDS,
        ge=30,
        le=7200,
    )
    # When true (default for force), stop live session immediately after arming.
    # Graceful always stops only after idle/deadline.
    stop_live_session: bool = True


class BundleDrainRequest(BaseModel):
    """Fan-out restart for every assistant mapped to a client bundle."""

    bundle_key: str
    environment: Literal["staging", "production"]
    target_revision: str
    mode: DrainMode = "graceful"
    deadline_seconds: int = Field(
        default=DEFAULT_GRACEFUL_DEADLINE_SECONDS,
        ge=30,
        le=7200,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sync_core_api():
    """Best-effort sync CoreV1 client for ConfigMap CAS (in-cluster / SA)."""
    from communication.infra.helpers import setup_kubernetes_client

    clients = setup_kubernetes_client()
    if clients is None or clients[1] is None:
        return None
    return clients[1]


def _read_drain_map(core_api) -> tuple[dict[str, Any], str | None]:
    try:
        config_map = core_api.read_namespaced_config_map(
            DRAIN_CONFIG_MAP,
            SETTINGS.default_namespace,
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        return {}, None
    data = dict(config_map.data or {})
    intents: dict[str, Any] = {}
    for key, raw in data.items():
        try:
            parsed = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed drain intent for assistant %s", key)
            continue
        if isinstance(parsed, dict):
            intents[str(key)] = parsed
    return intents, config_map.metadata.resource_version


def _write_drain_map(
    core_api,
    intents: dict[str, Any],
    *,
    resource_version: str | None,
) -> None:
    encoded = {
        key: json.dumps(value, sort_keys=True, separators=(",", ":"))
        for key, value in intents.items()
    }
    metadata = k8s_client.V1ObjectMeta(name=DRAIN_CONFIG_MAP)
    body = k8s_client.V1ConfigMap(metadata=metadata, data=encoded)
    if resource_version is None:
        try:
            core_api.create_namespaced_config_map(
                SETTINGS.default_namespace,
                body,
            )
            return
        except ApiException as exc:
            if exc.status != 409:
                raise
            _, resource_version = _read_drain_map(core_api)

    existing = core_api.read_namespaced_config_map(
        DRAIN_CONFIG_MAP,
        SETTINGS.default_namespace,
    )
    existing.data = encoded
    if resource_version:
        existing.metadata.resource_version = resource_version
    core_api.replace_namespaced_config_map(
        DRAIN_CONFIG_MAP,
        SETTINGS.default_namespace,
        existing,
    )


def get_drain_intent(assistant_id: str | int) -> DrainIntent | None:
    """Return the durable drain intent for ``assistant_id``, if any."""
    core_api = _sync_core_api()
    if core_api is None:
        return None
    intents, _ = _read_drain_map(core_api)
    raw = intents.get(str(assistant_id))
    if not isinstance(raw, dict):
        return None
    try:
        return DrainIntent.model_validate(raw)
    except Exception:  # noqa: BLE001 — treat corrupt rows as absent
        logger.warning("Invalid drain intent for assistant %s: %r", assistant_id, raw)
        return None


def set_drain_intent(intent: DrainIntent) -> DrainIntent:
    """Upsert a drain intent (CAS with a few retries)."""
    core_api = _sync_core_api()
    if core_api is None:
        raise HTTPException(
            status_code=503,
            detail="Kubernetes API unavailable for drain intent store",
        )
    aid = str(intent.assistant_id)
    payload = intent.model_dump(mode="json")
    last_error: Exception | None = None
    for _ in range(5):
        try:
            intents, resource_version = _read_drain_map(core_api)
            intents[aid] = payload
            _write_drain_map(core_api, intents, resource_version=resource_version)
            return intent
        except ApiException as exc:
            last_error = exc
            if exc.status not in {409, 404}:
                raise
            time.sleep(0.05)
    raise HTTPException(
        status_code=503,
        detail=f"Failed to persist drain intent: {last_error}",
    )


def clear_drain_intent(assistant_id: str | int) -> bool:
    """Remove the drain intent. Returns True if one was present."""
    core_api = _sync_core_api()
    if core_api is None:
        raise HTTPException(
            status_code=503,
            detail="Kubernetes API unavailable for drain intent store",
        )
    aid = str(assistant_id)
    for _ in range(5):
        intents, resource_version = _read_drain_map(core_api)
        if aid not in intents:
            return False
        intents.pop(aid, None)
        try:
            _write_drain_map(core_api, intents, resource_version=resource_version)
            return True
        except ApiException as exc:
            if exc.status not in {409, 404}:
                raise
            time.sleep(0.05)
    raise HTTPException(status_code=503, detail="Failed to clear drain intent")


def admission_blocked(assistant_id: str | int) -> DrainIntent | None:
    """Return the intent when new work must not start for this assistant."""
    intent = get_drain_intent(assistant_id)
    if intent is None:
        return None
    if not intent.block_admission:
        return None
    if intent.state in {"done", "failed"}:
        return None
    return intent


def assistants_for_bundle(
    *,
    bundle_key: str,
    environment: str,
) -> list[str]:
    """Assistant ids mapped to ``bundle_key`` in ``routing_manifest.yaml``."""
    from unify_deploy.assistant_deployments.routing_manifest import (
        load_routing_manifest,
    )

    manifest = load_routing_manifest()
    client_cfg = None
    for _name, cfg in (manifest.get("clients") or {}).items():
        if str(cfg.get("bundle_key") or _name) == bundle_key:
            client_cfg = cfg
            break
    if client_cfg is None:
        return []
    env_cfg = (client_cfg.get("environments") or {}).get(environment) or {}
    out: list[str] = []
    for target in env_cfg.get("targets") or []:
        if str(target.get("scope") or "") != "assistant":
            continue
        scope_id = target.get("scope_id")
        if scope_id not in (None, ""):
            out.append(str(scope_id))
    return out


def _normalize_assistant_label(assistant_id: str) -> str:
    return str(assistant_id).lower().replace("_", "-")[:63]


async def _list_active_job_names(
    batch_api,
    *,
    app_label: str,
    assistant_id: str,
) -> list[str]:
    sanitized = _normalize_assistant_label(assistant_id)
    jobs = await asyncio.to_thread(
        batch_api.list_namespaced_job,
        namespace=SETTINGS.default_namespace,
        label_selector=f"app={app_label},assistant-id={sanitized}",
    )
    return [
        job.metadata.name
        for job in jobs.items
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]


async def _busy_summary(assistant_id: str) -> dict[str, Any]:
    """Live conversation Jobs + offline task-execution Jobs still running."""
    batch_api, _, _, _ = await get_k8s_clients()
    if batch_api is None:
        return {
            "active_job_names": [],
            "active_offline_job_names": [],
            "busy": False,
            "unavailable": True,
        }
    active = await _list_active_job_names(
        batch_api,
        app_label="unity",
        assistant_id=assistant_id,
    )
    offline = await _list_active_job_names(
        batch_api,
        app_label="unity-task-execution",
        assistant_id=assistant_id,
    )
    return {
        "active_job_names": active,
        "active_offline_job_names": offline,
        "busy": bool(active or offline),
        "unavailable": False,
    }


async def _stop_live_session(
    assistant_id: str,
    *,
    source_reason: str,
) -> dict[str, Any]:
    """Best-effort session stop via the existing stop helper."""
    from communication.infra.assistant_sessions import (
        get_assistant_session,
        get_custom_objects_api,
    )
    from communication.infra.views import _persist_assistant_session_stop_request

    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        return {"stopped": False, "reason": "no_custom_api"}
    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
    )
    if session is None:
        return {"stopped": False, "reason": "no_session"}
    updated, binding_id = await asyncio.to_thread(
        _persist_assistant_session_stop_request,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
        session,
        source="drain_restart",
        source_reason=source_reason,
    )
    return {
        "stopped": True,
        "binding_id": binding_id,
        "phase": ((updated or {}).get("status") or {}).get("phase"),
    }


async def _graceful_waiter(assistant_id: str) -> None:
    """Poll busy-set until idle or deadline, then stop live and clear drain."""
    try:
        while True:
            intent = get_drain_intent(assistant_id)
            if intent is None:
                return
            if intent.state in {"done", "failed"}:
                return

            deadline = intent.deadline_at
            now = _utc_now()
            try:
                deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            except ValueError:
                deadline_dt = now

            busy = await _busy_summary(assistant_id)
            past_deadline = now >= deadline_dt

            if busy.get("busy") and not past_deadline:
                intent.state = "draining"
                set_drain_intent(intent)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Idle or deadline: stop live session, then clear admission.
            intent.state = "stopping"
            set_drain_intent(intent)
            if intent.mode == "force" or past_deadline or not busy.get("busy"):
                await _stop_live_session(
                    assistant_id,
                    source_reason=(
                        f"drain_{intent.mode}"
                        + ("_deadline" if past_deadline and busy.get("busy") else "")
                    ),
                )

            # Wait briefly for Jobs to go terminal after stop.
            for _ in range(36):  # ~3 minutes
                busy = await _busy_summary(assistant_id)
                if not busy.get("busy"):
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            intent = get_drain_intent(assistant_id)
            if intent is None:
                return
            intent.state = "done"
            intent.block_admission = False
            set_drain_intent(intent)
            # Clear so deferred offline dispatches can proceed on new code.
            clear_drain_intent(assistant_id)
            logger.info(
                "Drain restart complete for assistant %s (mode=%s reason=%s)",
                assistant_id,
                intent.mode,
                intent.reason,
            )
            return
    except Exception:  # noqa: BLE001 — surface on the intent row
        logger.exception("Graceful drain waiter failed for assistant %s", assistant_id)
        intent = get_drain_intent(assistant_id)
        if intent is not None:
            intent.state = "failed"
            intent.error = "waiter_exception"
            intent.block_admission = False
            try:
                set_drain_intent(intent)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to record drain failure for %s", assistant_id)
    finally:
        async with _waiter_lock:
            task = _waiter_tasks.get(assistant_id)
            if task is asyncio.current_task():
                _waiter_tasks.pop(assistant_id, None)


async def _ensure_waiter(assistant_id: str) -> None:
    async with _waiter_lock:
        existing = _waiter_tasks.get(assistant_id)
        if existing is not None and not existing.done():
            return
        _waiter_tasks[assistant_id] = asyncio.create_task(
            _graceful_waiter(assistant_id),
            name=f"drain-waiter-{assistant_id}",
        )


async def request_restart(
    assistant_id: str | int,
    body: RestartRequest,
) -> dict[str, Any]:
    """Arm drain/restart for one assistant."""
    aid = str(assistant_id)
    existing = get_drain_intent(aid)
    now = _utc_now()
    deadline = now.timestamp() + int(body.deadline_seconds)
    deadline_at = _iso(datetime.fromtimestamp(deadline, tz=timezone.utc))

    if existing is not None and existing.block_admission:
        # Idempotent: same/newer revision keeps the active drain.
        if (
            body.target_revision
            and existing.target_revision
            and body.target_revision == existing.target_revision
            and existing.state not in {"done", "failed"}
        ):
            return {
                "success": True,
                "status": "already_draining",
                "intent": existing.model_dump(mode="json"),
            }
        # Bump target revision / deadline on an in-flight drain.
        existing.target_revision = body.target_revision or existing.target_revision
        existing.deadline_at = deadline_at
        if body.mode == "force":
            existing.mode = "force"
        set_drain_intent(existing)
        if body.mode == "force":
            await _stop_live_session(aid, source_reason="drain_force_escalate")
        await _ensure_waiter(aid)
        return {
            "success": True,
            "status": "updated",
            "intent": existing.model_dump(mode="json"),
        }

    intent = DrainIntent(
        assistant_id=aid,
        mode=body.mode,
        reason=body.reason,
        target_revision=body.target_revision,
        requested_at=_iso(now),
        deadline_at=deadline_at,
        state="draining" if body.mode == "graceful" else "stopping",
        block_admission=True,
    )
    set_drain_intent(intent)

    if body.mode == "force" and body.stop_live_session:
        stop_result = await _stop_live_session(aid, source_reason="drain_force")
        # Force: wait briefly for Jobs to drain, then clear admission.
        await _ensure_waiter(aid)
        return {
            "success": True,
            "status": "force_armed",
            "intent": intent.model_dump(mode="json"),
            "stop": stop_result,
        }

    await _ensure_waiter(aid)
    return {
        "success": True,
        "status": "graceful_armed",
        "intent": intent.model_dump(mode="json"),
    }


async def request_bundle_drain(body: BundleDrainRequest) -> dict[str, Any]:
    """Arm graceful/force drain for every assistant mapped to a bundle."""
    assistants = assistants_for_bundle(
        bundle_key=body.bundle_key,
        environment=body.environment,
    )
    results: list[dict[str, Any]] = []
    for aid in assistants:
        result = await request_restart(
            aid,
            RestartRequest(
                mode=body.mode,
                reason="deploy",
                target_revision=body.target_revision,
                deadline_seconds=body.deadline_seconds,
            ),
        )
        results.append({"assistant_id": aid, **result})
    return {
        "success": True,
        "bundle_key": body.bundle_key,
        "environment": body.environment,
        "target_revision": body.target_revision,
        "assistants": assistants,
        "results": results,
    }


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@router.post("/assistants/{assistant_id}/restart")
async def restart_assistant_endpoint(
    assistant_id: str,
    body: RestartRequest,
):
    """Arm force or graceful restart (admin)."""
    return await request_restart(assistant_id, body)


@assistant_self_router.get("/assistants/{assistant_id}/restart")
async def get_restart_status_endpoint(assistant_id: str, request: Request):
    """Pod / ops readable drain status (admin or owning assistant)."""
    await authorize_admin_or_assistant(request, assistant_id=assistant_id)
    intent = get_drain_intent(assistant_id)
    busy = await _busy_summary(assistant_id)
    return {
        "assistant_id": str(assistant_id),
        "draining": intent is not None and bool(intent.block_admission),
        "intent": intent.model_dump(mode="json") if intent else None,
        "busy": busy,
    }


@router.delete("/assistants/{assistant_id}/restart")
async def cancel_restart_endpoint(assistant_id: str):
    """Cancel a pending drain if not yet stopping (admin)."""
    intent = get_drain_intent(assistant_id)
    if intent is None:
        return {"success": True, "status": "absent"}
    if intent.state in {"stopping", "restarting"}:
        raise HTTPException(
            status_code=409,
            detail="Drain already stopping; cannot cancel",
        )
    clear_drain_intent(assistant_id)
    async with _waiter_lock:
        task = _waiter_tasks.pop(str(assistant_id), None)
    if task is not None:
        task.cancel()
    return {"success": True, "status": "cancelled"}


@router.post("/assistants/drain-bundle")
async def drain_bundle_endpoint(body: BundleDrainRequest):
    """Publish fan-out: drain every assistant mapped to a client bundle."""
    return await request_bundle_drain(body)


__all__ = [
    "DRAIN_CONFIG_MAP",
    "DrainIntent",
    "RestartRequest",
    "BundleDrainRequest",
    "admission_blocked",
    "assistants_for_bundle",
    "clear_drain_intent",
    "get_drain_intent",
    "request_bundle_drain",
    "request_restart",
    "router",
    "set_drain_intent",
]
