"""Runtime identity helpers for assistant-scoped reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unify.session_details import SessionDetails


@dataclass(frozen=True)
class RuntimeIdentity:
    """Concrete assistant identity required for runtime materialization."""

    assistant_id: str
    user_id: str
    org_id: int | None = None
    team_ids: tuple[int, ...] = ()
    owner_team_id: int | None = None
    client_name: str | None = None
    deployment: str | None = None
    api_key: str | None = None

    @property
    def target_key(self) -> str:
        return f"assistant/{self.assistant_id}"


def runtime_identity_from_session(
    session_details: "SessionDetails",
    *,
    client_name: str | None = None,
    deployment: str | None = None,
) -> RuntimeIdentity:
    """Build runtime identity from the already-populated Unity session."""

    assistant_id = getattr(getattr(session_details, "assistant", None), "agent_id", "")
    user_id = getattr(getattr(session_details, "user", None), "id", "")
    api_key = getattr(session_details, "unify_key", None) or os.environ.get("UNIFY_KEY")
    return RuntimeIdentity(
        assistant_id=str(assistant_id or ""),
        user_id=str(user_id or ""),
        org_id=getattr(session_details, "org_id", None),
        team_ids=tuple(getattr(session_details, "team_ids", None) or ()),
        owner_team_id=getattr(session_details, "owner_team_id", None),
        client_name=client_name,
        deployment=deployment,
        api_key=api_key,
    )


def activate_runtime_context(identity: RuntimeIdentity) -> None:
    """Activate the current assistant's Unity/Unify runtime context."""

    if not identity.user_id:
        raise ValueError("Runtime reconciliation requires a concrete user_id")
    if not identity.assistant_id:
        raise ValueError("Runtime reconciliation requires a concrete assistant_id")
    if not identity.api_key:
        raise ValueError(
            "Runtime reconciliation requires the assistant-scoped UNIFY_KEY",
        )

    os.environ["UNIFY_KEY"] = identity.api_key

    from unify.session_details import SESSION_DETAILS
    from unify_deploy.infra.workers.worker_utils import activate_unify_context

    current_assistant_id = str(SESSION_DETAILS.assistant.agent_id or "")
    current_user_id = SESSION_DETAILS.user.id or ""
    if (
        current_assistant_id != identity.assistant_id
        or current_user_id != identity.user_id
        or SESSION_DETAILS.owner_team_id != identity.owner_team_id
    ):
        SESSION_DETAILS.populate(
            agent_id=(
                int(identity.assistant_id)
                if str(identity.assistant_id).isdigit()
                else None
            ),
            user_id=identity.user_id,
            org_id=identity.org_id,
            team_ids=list(identity.team_ids),
            owner_team_id=identity.owner_team_id,
        )
    SESSION_DETAILS.unify_key = identity.api_key
    SESSION_DETAILS.export_to_env()
    activate_unify_context(
        user_id=identity.user_id,
        assistant_id=identity.assistant_id,
    )
