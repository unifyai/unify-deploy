"""Enterprise startup hook for Unity.

Discovered at runtime via Python entry points when the
``_UNITY_STARTUP_HOOK_GROUP`` environment variable is set to the
group name declared in this package's ``pyproject.toml``.

Performs runtime hydration tasks that were previously steps 7-9 in
``_init_managers``:

1. Resolve client customization (deployment-matched spec; shared seed layers merged org→team→user→assistant; secrets from ``.secrets.json`` applied last)
2. Sync seed data (contacts, guidance, knowledge, secrets, blacklist)
3. Sync custom functions and virtual environments

Deploy-time control-plane metadata, such as Console ``console_config``, is
primarily reconciled by ``unity_deploy.scripts.reconcile_control_plane``.  The
hook keeps a best-effort idempotent PATCH as drift repair for assistants that
wake after a deployment spec changes.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from unity_deploy.utils.orchestra_client import OrchestraClientError, patch_json

if TYPE_CHECKING:
    from unity.conversation_manager.conversation_manager import ConversationManager
    from unity.session_details import SessionDetails

logger = logging.getLogger(__name__)


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


def startup_hook(
    cm: "ConversationManager",
    session_details: "SessionDetails",
) -> dict[str, Any] | None:
    """Enterprise runtime hydration hook called during ``_init_managers``.

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
    from unity_deploy.customization.clients import resolve
    from unity_deploy.customization.integrations.activation import expand_integrations
    from unity_deploy.customization.seed_sync import sync_all_seed_data
    from unity_deploy.runtime import get_runtime_backend_overrides
    from unity.function_manager.custom_functions import (
        collect_functions_from_directories,
        collect_venvs_from_directories,
    )
    from unity.manager_registry import ManagerRegistry

    resolved = resolve(
        org_id=session_details.org_id,
        team_ids=session_details.team_ids or None,
        user_id=session_details.user.id,
        assistant_id=session_details.assistant.agent_id,
    )
    resolved = expand_integrations(resolved)

    sync_all_seed_data(resolved)

    if resolved.console_config:
        _sync_console_config(
            session_details.assistant.agent_id,
            resolved.console_config,
        )

    if resolved.function_dirs or resolved.venv_dirs:
        source_fns = collect_functions_from_directories(resolved.function_dirs)
        source_venvs = collect_venvs_from_directories(resolved.venv_dirs)
        fm = ManagerRegistry.get_function_manager()
        if source_fns or source_venvs:
            fm.sync_custom(source_functions=source_fns, source_venvs=source_venvs)

    if resolved.mcp_configs:
        logger.info(
            "Loaded %d MCP integration config(s); runtime MCP wrapper sync is deferred",
            len(resolved.mcp_configs),
        )

    config = resolved.config
    url_mappings = dict(config.url_mappings or {})
    url_mappings.update(resolved.url_mappings)
    return {
        "environments": resolved.environments,
        "url_mappings": url_mappings or None,
        "runtime_backends": get_runtime_backend_overrides(),
        "actor_kwargs": {
            k: v
            for k, v in {
                "can_compose": config.can_compose,
                "can_store": config.can_store,
                "timeout": config.timeout,
                "model": config.model,
                "prompt_caching": config.prompt_caching,
                "guidelines": config.guidelines,
            }.items()
            if v is not None
        },
    }
