from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from communication.infra.assistant_sessions import (
    assistant_session_desired_state,
    binding_id as binding_id_from_status,
    session_binding,
    session_desktop_mode,
    session_desktop_required,
)


@dataclass(frozen=True)
class SessionSnapshot:
    """Immutable view of the controller-relevant session state."""

    body: dict[str, Any]
    session_name: str
    assistant_id: str
    activation_id: str
    desired_state: str
    desktop_required: bool
    desktop_mode: str
    secret_name: str
    observed_activation_id: str
    existing_conditions: list[dict[str, Any]]
    binding: dict[str, Any]
    current_binding_id: str
    phase: str
    bootstrap_retries: int
    vm_retries: int
    desktop_probe_failures: int
    last_error: str

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> "SessionSnapshot":
        metadata = body.get("metadata", {})
        spec = body.get("spec", {})
        status = body.get("status", {})
        binding = session_binding(body)
        return cls(
            body=body,
            session_name=str(metadata.get("name", "") or ""),
            assistant_id=str(spec.get("assistantId", "") or ""),
            activation_id=str(spec.get("activationId", "") or ""),
            desired_state=assistant_session_desired_state(body),
            desktop_required=session_desktop_required(body),
            desktop_mode=session_desktop_mode(body),
            secret_name=str(spec.get("startupSecretRef", "") or ""),
            observed_activation_id=str(status.get("observedActivationId", "") or ""),
            existing_conditions=status.get("conditions", []),
            binding=binding,
            current_binding_id=binding_id_from_status(binding),
            phase=str(status.get("phase", "") or ""),
            bootstrap_retries=int(status.get("bootstrapRetries", 0) or 0),
            vm_retries=int(status.get("vmRetries", 0) or 0),
            desktop_probe_failures=int(
                status.get("desktopProbeFailures", 0) or 0,
            ),
            last_error=str(status.get("lastError", "") or ""),
        )
