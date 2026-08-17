"""Deployment runtime bootstrap shared by live and offline assistant lanes.

Live assistants invoke this from the ConversationManager startup hook.
Offline jobs invoke the same path without a ConversationManager so reconcile,
client-bundle resolution, and actor startup config stay in parity.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from time import perf_counter
from typing import Any, TYPE_CHECKING

from unify.knowledge_manager.custom_knowledge import (
    collect_knowledge_from_directories,
    knowledge_titles_from_source,
)
from unify.data_manager.custom_data import list_data_table_contexts
from unify.logger import LOGGER as logger
from unify_deploy.timing import log_startup_timing
from unify_deploy.utils.orchestra_client import OrchestraClientError, patch_json

if TYPE_CHECKING:
    from unify.conversation_manager.conversation_manager import ConversationManager
    from unify.session_details import SessionDetails


@contextmanager
def _timed_hook_phase(name: str):
    start = perf_counter()
    try:
        yield
    finally:
        logger.info(
            "Enterprise startup hook phase '%s' completed in %.2fs",
            name,
            perf_counter() - start,
        )


def _sync_console_config(
    assistant_id: int | str,
    console_config: dict[str, Any],
) -> None:
    """Best-effort drift repair for assistant ``console_config``.

    Primary first-visibility sync happens at deploy time via the control-plane
    reconciler.  This wake-time PATCH is intentionally non-blocking so runtime
    startup does not fail if Orchestra is temporarily unavailable.
    """
    try:
        from unify.session_details import SESSION_DETAILS

        patch_json(
            f"/assistant/{assistant_id}/runtime-profile",
            {"console_config": console_config},
            auth_token=SESSION_DETAILS.unify_key or None,
        )
        logger.info("Synced console_config for assistant %s", assistant_id)
    except OrchestraClientError:
        logger.warning(
            "Failed to sync console_config for assistant %s",
            assistant_id,
            exc_info=True,
        )
        logging.getLogger(__name__).warning(
            "Failed to sync console_config for assistant %s",
            assistant_id,
            exc_info=True,
        )


def _normalize_reconcile_mode(reconcile_mode: str | None) -> str:
    if reconcile_mode is None:
        reconcile_mode = os.environ.get(
            "UNITY_DEPLOY_RUNTIME_RECONCILE_MODE",
            "async",
        )
    normalized = str(reconcile_mode).strip().lower()
    if normalized not in {"async", "off", "blocking"}:
        logger.warning(
            "Unknown runtime reconcile mode %r; using async",
            reconcile_mode,
        )
        return "async"
    return normalized


def ensure_deployment_runtime(
    session_details: "SessionDetails",
    cm: "ConversationManager | None" = None,
    *,
    reconcile_mode: str | None = None,
) -> dict[str, Any] | None:
    """Resolve deployment spec, reconcile runtime state, build actor config.

    Parameters
    ----------
    session_details:
        Runtime identity carrying org/team/user/assistant and UNIFY_KEY.
    cm:
        Optional ConversationManager. Offline jobs pass ``None``; reconcile
        already tolerates a missing CM (no status attribute / notifications).
    reconcile_mode:
        ``async`` / ``blocking`` / ``off``. When ``None``, reads
        ``UNITY_DEPLOY_RUNTIME_RECONCILE_MODE`` (default ``async``). Offline
        callers should pass ``blocking`` so Secrets/functions land before the
        task entrypoint runs.
    """
    from unify_deploy.startup_config import (
        StartupIdentity,
        build_actor_startup_config,
        expand_startup_integrations,
        resolve_startup_spec,
    )

    assistant_id = session_details.assistant.agent_id
    identity = StartupIdentity(
        assistant_id=str(assistant_id),
        user_id=session_details.user.id,
        org_id=session_details.org_id,
        team_ids=tuple(session_details.team_ids or ()),
    )
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] unify_deploy.ensure_deployment_runtime identity "
        "assistant=%s user=%s org=%s teams=%d",
        identity.assistant_id,
        identity.user_id,
        identity.org_id,
        len(identity.team_ids),
    )
    with _timed_hook_phase("resolve"):
        resolved = resolve_startup_spec(identity)
    log_startup_timing(
        logger,
        (
            "⏱️ [StartupTiming] unify_deploy.ensure_deployment_runtime resolved "
            "contacts=%d secrets_dirs=%d supplemental_secrets=%d guidance_dirs=%d "
            "knowledge_claims=%d custom_data_tables=%d "
            "tasks_dirs=%d files_dirs=%d blacklist_dirs=%d "
            "function_dirs=%d venv_dirs=%d integrations=%d"
        ),
        len(resolved.contacts_dirs),
        len(resolved.secrets_dirs),
        len(resolved.secrets),
        len(resolved.guidance_dirs),
        len(
            knowledge_titles_from_source(
                collect_knowledge_from_directories(resolved.knowledge_dirs),
            ),
        ),
        len(list_data_table_contexts(resolved.custom_data_dirs)),
        len(resolved.tasks_dirs),
        len(resolved.files_dirs),
        len(resolved.blacklist_dirs),
        len(resolved.function_dirs),
        len(resolved.venv_dirs),
        len(resolved.integrations),
    )
    with _timed_hook_phase("expand_integrations"):
        resolved = expand_startup_integrations(resolved)

    runtime_reconcile_mode = _normalize_reconcile_mode(reconcile_mode)
    if runtime_reconcile_mode == "blocking":
        logger.warning(
            "Running explicit blocking runtime reconciliation for assistant %s",
            assistant_id,
        )

    from unify_deploy.runtime_reconcile.context import runtime_identity_from_session
    from unify_deploy.runtime_reconcile.runner import (
        RuntimeReconcileHandle,
        start_runtime_reconcile,
    )
    from unify_deploy.runtime_reconcile.status import (
        RuntimeReconcileStatusHandle,
        runtime_reconcile_prompt_note,
    )

    try:
        with _timed_hook_phase("start_runtime_reconcile"):
            reconcile_handle = start_runtime_reconcile(
                cm,
                resolved,
                runtime_identity_from_session(session_details),
                mode=runtime_reconcile_mode,
            )
    except Exception as exc:
        logger.exception(
            "Failed to schedule runtime reconciliation for assistant %s; continuing degraded",
            assistant_id,
        )
        logging.getLogger(__name__).exception(
            "Failed to schedule runtime reconciliation for assistant %s; continuing degraded",
            assistant_id,
        )
        status = RuntimeReconcileStatusHandle()
        status.update(
            phase="failed",
            message="Background assistant setup failed to start.",
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
        if cm is not None:
            setattr(cm, "deployment_runtime_reconcile_status", status)
        reconcile_handle = RuntimeReconcileHandle(status=status)

    if resolved.console_config and os.environ.get(
        "UNIFY_DEPLOY_WAKE_CONSOLE_REPAIR",
        "",
    ).lower() in {"1", "true", "yes"}:
        with _timed_hook_phase("sync_console_config"):
            _sync_console_config(
                assistant_id,
                resolved.console_config,
            )

    if resolved.mcp_configs:
        logger.info(
            "Loaded %d MCP integration config(s); runtime MCP wrapper sync is deferred",
            len(resolved.mcp_configs),
        )

    with _timed_hook_phase("build_actor_startup_config"):
        actor_config = build_actor_startup_config(resolved)

    setup_note = runtime_reconcile_prompt_note(reconcile_handle.status)
    if setup_note:
        actor_kwargs = actor_config.setdefault("actor_kwargs", {})
        actor_kwargs["guidelines"] = "\n\n".join(
            filter(
                None,
                [
                    actor_kwargs.get("guidelines"),
                    setup_note,
                ],
            ),
        )

    return actor_config


__all__ = [
    "ensure_deployment_runtime",
    "_sync_console_config",
]
