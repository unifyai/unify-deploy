"""Enterprise startup hook for Unity.

Discovered at runtime via Python entry points when the
``_UNITY_STARTUP_HOOK_GROUP`` environment variable is set to the
group name declared in this package's ``pyproject.toml``.

Keeps the wake-time path intentionally thin:

1. Resolve assistant deployment spec (deployment-matched spec; shared seed layers merged org→team→user→assistant; secrets from ``.secrets.json`` applied last)
2. Expand integrations into in-memory runtime config.
3. Build actor startup config.

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

from unity.logger import LOGGER as logger
from unity_deploy.utils.orchestra_client import OrchestraClientError, patch_json

if TYPE_CHECKING:
    from unity.conversation_manager.conversation_manager import ConversationManager
    from unity.session_details import SessionDetails


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
        Runtime identity carrying org_id, team_ids, user.id, assistant.agent_id.
        Console control-plane state should already have been reconciled before
        this hook runs.

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
    with _timed_hook_phase("resolve"):
        resolved = resolve_startup_spec(identity)
    with _timed_hook_phase("expand_integrations"):
        resolved = expand_startup_integrations(resolved)

    wake_hydration_mode = (
        os.environ.get(
            "UNITY_DEPLOY_WAKE_HYDRATION_MODE",
            "off",
        )
        .strip()
        .lower()
    )
    if wake_hydration_mode not in {"off", "blocking"}:
        logger.warning(
            "Unknown UNITY_DEPLOY_WAKE_HYDRATION_MODE=%r; using off",
            wake_hydration_mode,
        )
        wake_hydration_mode = "off"

    if wake_hydration_mode == "blocking":
        from unity_deploy.deployment_reconcile.runtime_state import (
            RuntimeIdentity,
            materialize_runtime_state,
        )

        logger.warning(
            "Running explicit blocking runtime state repair for assistant %s",
            assistant_id,
        )
        with _timed_hook_phase("materialize_runtime_state"):
            materialize_runtime_state(
                resolved,
                RuntimeIdentity(
                    assistant_id=identity.assistant_id,
                    user_id=identity.user_id,
                    org_id=identity.org_id,
                    team_ids=identity.team_ids,
                ),
            )

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
        return build_actor_startup_config(resolved)
