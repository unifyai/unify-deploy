"""Status contract for assistant-scoped runtime reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Literal

RuntimeReconcilePhase = Literal[
    "not_started",
    "starting",
    "syncing_seed_data",
    "syncing_custom_functions",
    "complete",
    "failed",
]

ResourceState = Literal["pending", "syncing", "ready", "failed", "skipped"]

INCOMPLETE_PHASES = {
    "not_started",
    "starting",
    "syncing_seed_data",
    "syncing_custom_functions",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RuntimeReconcileStatus:
    """Serializable status for background assistant setup."""

    current_phase: RuntimeReconcilePhase = "not_started"
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    last_message: str = ""
    error: str = ""
    blocking_resources: tuple[str, ...] = ()
    resources: dict[str, ResourceState] | None = None
    can_answer_general_questions: bool = True
    data_freshness: Literal["unknown", "partial", "ready", "failed"] = "unknown"

    @property
    def phase(self) -> RuntimeReconcilePhase:
        return self.current_phase

    @property
    def message(self) -> str:
        return self.last_message

    @property
    def is_running(self) -> bool:
        return self.current_phase in INCOMPLETE_PHASES

    @property
    def is_complete(self) -> bool:
        return self.current_phase == "complete"

    @property
    def has_failed(self) -> bool:
        return self.current_phase == "failed"


class RuntimeReconcileStatusHandle:
    """Thread-safe owner for one assistant's runtime reconciliation status."""

    def __init__(self, status: RuntimeReconcileStatus | None = None) -> None:
        self._lock = RLock()
        self._status = status or RuntimeReconcileStatus()

    def snapshot(self) -> RuntimeReconcileStatus:
        with self._lock:
            resources = self._status.resources
            return replace(
                self._status,
                resources=dict(resources) if resources is not None else None,
            )

    def update(
        self,
        *,
        phase: RuntimeReconcilePhase | None = None,
        message: str | None = None,
        error: str | None = None,
        blocking_resources: tuple[str, ...] | list[str] | None = None,
        resources: dict[str, ResourceState] | None = None,
        can_answer_general_questions: bool | None = None,
        data_freshness: Literal["unknown", "partial", "ready", "failed"] | None = None,
    ) -> RuntimeReconcileStatus:
        with self._lock:
            now = utc_now()
            next_phase = phase or self._status.current_phase
            started_at = self._status.started_at
            finished_at = self._status.finished_at
            if next_phase != "not_started" and started_at is None:
                started_at = now
            if next_phase in {"complete", "failed"}:
                finished_at = now

            self._status = RuntimeReconcileStatus(
                current_phase=next_phase,
                started_at=started_at,
                updated_at=now,
                finished_at=finished_at,
                last_message=(
                    self._status.last_message if message is None else message
                ),
                error=self._status.error if error is None else error,
                blocking_resources=(
                    self._status.blocking_resources
                    if blocking_resources is None
                    else tuple(blocking_resources)
                ),
                resources=(
                    self._status.resources if resources is None else dict(resources)
                ),
                can_answer_general_questions=(
                    self._status.can_answer_general_questions
                    if can_answer_general_questions is None
                    else can_answer_general_questions
                ),
                data_freshness=(
                    self._status.data_freshness
                    if data_freshness is None
                    else data_freshness
                ),
            )
            return self._status


INCOMPLETE_SETUP_NOTE = (
    "Some assistant setup is still finishing in the background. "
    "Deployment-defined data, guidance, secrets, or custom tools may still be "
    "syncing. You can answer general questions normally. If the user asks for "
    "information that depends on not-yet-ready data or tools, be transparent: "
    "say that those systems are still being prepared, avoid claiming fresh "
    "results, and offer to continue once setup completes."
)

FAILED_SETUP_NOTE = (
    "Background assistant setup failed. Some deployment-defined data or custom "
    "tools may be unavailable. If the user asks about affected capabilities, "
    "explain that setup hit an error and avoid pretending unavailable data/tools "
    "are ready."
)

PARTIAL_FUNCTIONS_SETUP_NOTE = (
    "Background assistant setup finished, but some deployment-defined custom "
    "functions failed to sync. Other data and tools are ready. If the user asks "
    "about a missing or broken custom tool, explain that that function failed "
    "during setup and will be retried on the next reconcile — do not pretend it "
    "is available."
)

COMPLETE_SETUP_NOTE = (
    "Background assistant setup is complete. Deployment-defined data, guidance, "
    "secrets, and custom tools are ready."
)


def runtime_reconcile_prompt_note(
    status: RuntimeReconcileStatus | RuntimeReconcileStatusHandle | None,
    *,
    include_complete: bool = False,
) -> str | None:
    """Translate internal status into concise assistant-facing guidance."""

    if status is None:
        return None
    if isinstance(status, RuntimeReconcileStatusHandle):
        status = status.snapshot()

    if status.has_failed:
        return FAILED_SETUP_NOTE
    if status.is_complete:
        resources = status.resources or {}
        if resources.get("functions") == "failed" or status.error:
            return PARTIAL_FUNCTIONS_SETUP_NOTE
        return COMPLETE_SETUP_NOTE if include_complete else None
    if status.is_running:
        return INCOMPLETE_SETUP_NOTE
    return None
