"""Enterprise startup hook for Unity.

Discovered at runtime via Python entry points when the
``_UNITY_STARTUP_HOOK_GROUP`` environment variable is set to the
group name declared in this package's ``pyproject.toml``.

Keeps the wake-time path intentionally thin:

1. Resolve assistant deployment spec (deployment-matched spec; shared seed layers merged org/team/user/assistant; secrets from ``.secrets.json`` applied last)
2. Expand integrations into in-memory runtime config.
3. Start assistant-scoped runtime reconciliation in the background.
4. Build actor startup config.

Deploy-time control-plane metadata, such as Console ``console_config``, is
primarily reconciled by ``unity_deploy.scripts.reconcile_deployment`` with the
``control-plane`` plane enabled.  The hook keeps a best-effort idempotent PATCH
as drift repair for assistants that wake after a deployment spec changes.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from time import perf_counter
from typing import Any, TYPE_CHECKING

from unify.logger import LOGGER as logger
from unity_deploy.timing import log_startup_timing
from unity_deploy.utils.orchestra_client import OrchestraClientError, patch_json

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
        patch_json(
            f"/admin/assistant/{assistant_id}",
            {"console_config": console_config},
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


def startup_hook(
    cm: "ConversationManager",
    session_details: "SessionDetails",
) -> dict[str, Any] | None:
    """Enterprise startup config hook called during ``_init_managers``.

    Parameters
    ----------
    cm : ConversationManager
        The conversation manager instance (fully constructed minus the Actor).
    session_details : SessionDetails
        Runtime identity carrying org_id, team_ids, user.id, assistant.agent_id,
        and the assistant-scoped UNIFY_KEY. Console control-plane state should
        already have been reconciled before this hook runs.

    Returns
    -------
    dict | None
        Configuration consumed by ``_init_managers`` for Actor construction:
        - ``environments``: extra execution environments
        - ``url_mappings``: URL rewrites for ComputerPrimitives
        - ``actor_kwargs``: kwargs passed to ``CodeActActor.__init__``
    """
    from unity_deploy.startup_config import (
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
        "⏱️ [StartupTiming] unity_deploy.startup_hook identity assistant=%s user=%s org=%s teams=%d",
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
            "⏱️ [StartupTiming] unity_deploy.startup_hook resolved "
            "contacts=%d guidance_dirs=%d knowledge_tables=%d secrets=%d blacklist_dirs=%d "
            "function_dirs=%d venv_dirs=%d integrations=%d"
        ),
        len(resolved.contacts_dirs),
        len(resolved.guidance_dirs),
        len(resolved.knowledge),
        len(resolved.secrets),
        len(resolved.blacklist_dirs),
        len(resolved.function_dirs),
        len(resolved.venv_dirs),
        len(resolved.integrations),
    )
    with _timed_hook_phase("expand_integrations"):
        resolved = expand_startup_integrations(resolved)

    runtime_reconcile_mode = (
        os.environ.get("UNITY_DEPLOY_RUNTIME_RECONCILE_MODE", "async").strip().lower()
    )
    if runtime_reconcile_mode not in {"async", "off", "blocking"}:
        logger.warning(
            "Unknown runtime reconcile mode %r; using async",
            runtime_reconcile_mode,
        )
        runtime_reconcile_mode = "async"

    if runtime_reconcile_mode == "blocking":
        logger.warning(
            "Running explicit blocking runtime reconciliation for assistant %s",
            assistant_id,
        )

    from unity_deploy.runtime_reconcile.context import runtime_identity_from_session
    from unity_deploy.runtime_reconcile.runner import (
        RuntimeReconcileHandle,
        start_runtime_reconcile,
    )
    from unity_deploy.runtime_reconcile.status import (
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
        "UNITY_DEPLOY_WAKE_CONSOLE_REPAIR",
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
