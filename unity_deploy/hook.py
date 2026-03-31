"""Enterprise startup hook for Unity.

Discovered at runtime via Python entry points when the
``_UNITY_STARTUP_HOOK_GROUP`` environment variable is set to the
group name declared in this package's ``pyproject.toml``.

Performs three tasks that were previously steps 7-9 in
``_init_managers``:

1. Resolve client customization (org/team/user/assistant cascade)
2. Sync seed data (contacts, guidance, knowledge, secrets, blacklist)
3. Sync custom functions and virtual environments
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from unity.conversation_manager.conversation_manager import ConversationManager
    from unity.session_details import SessionDetails

logger = logging.getLogger(__name__)


def startup_hook(
    cm: "ConversationManager",
    session_details: "SessionDetails",
) -> dict[str, Any] | None:
    """Enterprise startup hook called during ``_init_managers``.

    Parameters
    ----------
    cm : ConversationManager
        The conversation manager instance (fully constructed minus the Actor).
    session_details : SessionDetails
        Runtime identity carrying org_id, team_ids, user.id, assistant.agent_id.

    Returns
    -------
    dict | None
        Configuration consumed by ``_init_managers`` for Actor construction:
        - ``environments``: extra execution environments
        - ``url_mappings``: URL rewrites for ComputerPrimitives
        - ``actor_kwargs``: kwargs passed to ``CodeActActor.__init__``
    """
    from unity_deploy.customization.clients import resolve
    from unity_deploy.customization.seed_sync import sync_all_seed_data
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

    sync_all_seed_data(resolved)

    if resolved.function_dirs or resolved.venv_dirs:
        source_fns = collect_functions_from_directories(resolved.function_dirs)
        source_venvs = collect_venvs_from_directories(resolved.venv_dirs)
        fm = ManagerRegistry.get_function_manager()
        if source_fns or source_venvs:
            fm.sync_custom(source_functions=source_fns, source_venvs=source_venvs)

    config = resolved.config
    return {
        "environments": resolved.environments,
        "url_mappings": config.url_mappings if config.url_mappings else None,
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
