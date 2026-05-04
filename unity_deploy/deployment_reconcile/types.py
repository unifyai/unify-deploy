"""Shared types for deploy-time reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

Plane = Literal["control-plane", "runtime"]
VALID_PLANES: set[str] = {"control-plane", "runtime"}


@dataclass(frozen=True)
class DeploymentTargetPlan:
    """Resolved deployment target for one assistant."""

    environment: str
    client_name: str
    deployment: str
    assistant_id: str
    resolved: Any
    control_plane_operations: tuple[Any, ...] = ()
    runtime_revision: str = ""
    control_plane_revision: str = ""
    runtime_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def target_key(self) -> str:
        return (
            f"{self.environment}/{self.client_name}/"
            f"{self.deployment}/{self.assistant_id}"
        )


@dataclass(frozen=True)
class DeploymentWorkItem:
    """One independently executable deploy-time work item."""

    target: DeploymentTargetPlan
    plane: Plane
    revision: str
    idempotency_key: str

    @property
    def target_key(self) -> str:
        return self.target.target_key


@dataclass(frozen=True)
class DeploymentWorkResult:
    """Result for one deploy-time work item."""

    item: DeploymentWorkItem
    status: Literal["planned", "applied", "skipped-current", "failed"]
    message: str = ""
    error: str = ""


def parse_planes(value: str | Iterable[str] | None) -> tuple[Plane, ...]:
    """Normalize CLI plane input into canonical plane names."""

    raw = "control-plane" if value is None else value
    if isinstance(raw, str):
        candidates = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        candidates = [str(p).strip() for p in raw if str(p).strip()]
    if not candidates:
        raise ValueError("At least one deployment reconciliation plane is required")
    unknown = sorted(set(candidates) - VALID_PLANES)
    if unknown:
        raise ValueError(f"Unknown deployment plane(s): {', '.join(unknown)}")
    return tuple(candidates)  # type: ignore[return-value]
