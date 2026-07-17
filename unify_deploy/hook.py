"""Enterprise startup hook for Unity.

Discovered at runtime via Python entry points when the
``_UNITY_STARTUP_HOOK_GROUP`` environment variable is set to the
group name declared in this package's ``pyproject.toml``.

Keeps the wake-time path intentionally thin by delegating to
``unify_deploy.runtime_bootstrap.ensure_deployment_runtime`` — the same
entry offline jobs use without a ConversationManager.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from unify_deploy.runtime_bootstrap import (
    _sync_console_config,
    ensure_deployment_runtime,
)

if TYPE_CHECKING:
    from unify.conversation_manager.conversation_manager import ConversationManager
    from unify.session_details import SessionDetails

# Re-exported for tests and any callers that imported the private helper.
__all__ = [
    "startup_hook",
    "_sync_console_config",
    "ensure_deployment_runtime",
]


def startup_hook(
    cm: "ConversationManager",
    session_details: "SessionDetails",
) -> dict[str, Any] | None:
    """Enterprise startup config hook called during ``_init_managers``.

    Parameters
    ----------
    cm : ConversationManager
        The conversation manager instance (fully constructed minus the Actor).
        Offline jobs call ``ensure_deployment_runtime`` with ``cm=None`` instead.
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
    return ensure_deployment_runtime(
        session_details=session_details,
        cm=cm,
    )
