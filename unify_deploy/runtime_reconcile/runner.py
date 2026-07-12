"""Background orchestration for assistant-scoped runtime reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import threading
from time import perf_counter
from typing import TYPE_CHECKING

from unify.logger import LOGGER as logger
from unify_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unify_deploy.runtime_reconcile.context import (
    RuntimeIdentity,
    activate_runtime_context,
)
from unify_deploy.runtime_reconcile.materialize import materialize_runtime_state
from unify_deploy.runtime_reconcile.status import RuntimeReconcileStatusHandle
from unify_deploy.timing import log_startup_timing

if TYPE_CHECKING:
    from unify.conversation_manager.conversation_manager import ConversationManager


@dataclass(frozen=True)
class RuntimeReconcileHandle:
    """Handle returned after scheduling runtime reconciliation."""

    status: RuntimeReconcileStatusHandle
    thread: threading.Thread | None = None


def _push_setup_notification(
    cm: "ConversationManager | None",
    message: str,
) -> None:
    if cm is None:
        return

    def _notify() -> None:
        notifications = getattr(cm, "notifications_bar", None)
        if notifications is not None:
            notifications.push_notif(
                "System",
                message,
                datetime.now(timezone.utc),
            )
        request_llm_run = getattr(cm, "request_llm_run", None)
        if request_llm_run is not None:
            result = None
            try:
                result = request_llm_run(delay=0)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except RuntimeError:
                if asyncio.iscoroutine(result):
                    result.close()
                logger.debug(
                    "Could not request LLM run after runtime reconcile notification",
                    exc_info=True,
                )

    loop = getattr(cm, "loop", None)
    if loop is not None and getattr(loop, "is_running", lambda: False)():
        loop.call_soon_threadsafe(_notify)
    else:
        _notify()


def _run_runtime_reconcile(
    *,
    cm: "ConversationManager | None",
    resolved: ResolvedAssistantDeployment,
    identity: RuntimeIdentity,
    status: RuntimeReconcileStatusHandle,
    revision: str | None = None,
) -> None:
    logger.info(
        "Starting assistant-scoped runtime reconciliation for assistant %s",
        identity.assistant_id,
    )
    reconcile_start = perf_counter()
    try:
        status.update(
            phase="starting",
            message="Preparing deployment-defined data and custom tools.",
            blocking_resources=("contacts", "guidance", "knowledge", "functions"),
            resources={
                "contacts": "pending",
                "guidance": "pending",
                "knowledge": "pending",
                "secrets": "pending",
                "functions": "pending",
            },
            data_freshness="partial",
        )
        context_start = perf_counter()
        activate_runtime_context(identity)
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.activate_runtime_context assistant=%s duration=%.2fs",
            identity.assistant_id,
            perf_counter() - context_start,
        )
        materialize_runtime_state(
            resolved,
            identity,
            revision=revision,
            status=status,
        )
        _push_setup_notification(
            cm,
            "Assistant setup complete; deployment-defined data and custom tools are ready.",
        )
        logger.info(
            "Assistant-scoped runtime reconciliation completed for assistant %s",
            identity.assistant_id,
        )
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.total assistant=%s duration=%.2fs",
            identity.assistant_id,
            perf_counter() - reconcile_start,
        )
    except Exception as exc:
        status.update(
            phase="failed",
            message="Background assistant setup failed.",
            error=str(exc),
            blocking_resources=("contacts", "guidance", "knowledge", "functions"),
            resources={
                "contacts": "failed",
                "guidance": "failed",
                "knowledge": "failed",
                "secrets": "failed",
                "functions": "failed",
            },
            data_freshness="failed",
        )
        _push_setup_notification(
            cm,
            "Assistant setup hit an error; some deployment-defined data or custom tools may be unavailable.",
        )
        logger.exception(
            "Assistant-scoped runtime reconciliation failed for assistant %s",
            identity.assistant_id,
        )


def start_runtime_reconcile(
    cm: "ConversationManager | None",
    resolved: ResolvedAssistantDeployment,
    identity: RuntimeIdentity,
    *,
    mode: str = "async",
    revision: str | None = None,
) -> RuntimeReconcileHandle:
    """Start runtime reconciliation for the current assistant."""

    normalized_mode = mode.strip().lower()
    status = RuntimeReconcileStatusHandle()
    if cm is not None:
        setattr(cm, "deployment_runtime_reconcile_status", status)

    logger.info(
        "Runtime reconcile scheduling: assistant=%s user=%s mode=%s",
        identity.assistant_id,
        identity.user_id,
        normalized_mode,
    )

    if normalized_mode == "off":
        status.update(
            phase="not_started",
            message="Background assistant setup is disabled.",
            data_freshness="unknown",
        )
        return RuntimeReconcileHandle(status=status)

    if normalized_mode == "blocking":
        _run_runtime_reconcile(
            cm=cm,
            resolved=resolved,
            identity=identity,
            status=status,
            revision=revision,
        )
        return RuntimeReconcileHandle(status=status)

    if normalized_mode != "async":
        logger.warning(
            "Unknown runtime reconcile mode %r; using async",
            mode,
        )

    thread = threading.Thread(
        target=_run_runtime_reconcile,
        kwargs={
            "cm": cm,
            "resolved": resolved,
            "identity": identity,
            "status": status,
            "revision": revision,
        },
        name=f"unity-deploy-runtime-reconcile-{identity.assistant_id}",
        daemon=True,
    )
    thread.start()
    return RuntimeReconcileHandle(status=status, thread=thread)
