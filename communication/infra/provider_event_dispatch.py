"""Offline provider-event dispatch helpers for Communication.

# TODO: Remove the inbox dependency from ``dispatch_provider_event_offline`` once
Orchestra-backed downstream adoption is wired; keep request validation / audience.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from communication.infra.provider_event_dispatch_inbox import (
    DispatchInboxSnapshot,
    ProviderEventDispatchInbox,
)

PROVIDER_EVENT_DISPATCH_AUDIENCE = "communication:provider-event-dispatch"
PublicDispatchStatus = Literal["adopted", "started", "terminal"]


class ProviderEventDispatchRequest(BaseModel):
    """Internal dispatch authorization for provider-event offline execution."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"] = "1"
    operation_id: str
    run_id: int
    run_key: str
    assistant_id: str
    task_id: int
    binding_id: str
    receipt_id: str
    accepted_activation_revision: str
    source_type: Literal["provider_event"] = "provider_event"
    dispatch_mode: Literal["live", "offline"] = "offline"
    event_context_ref: str
    issued_at: datetime
    audience: str = Field(default=PROVIDER_EVENT_DISPATCH_AUDIENCE)


class ProviderEventDispatchValidationError(ValueError):
    """Raised when one dispatch request fails authorization validation."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class ProviderEventDispatchOutcome:
    """Result of one provider-event dispatch attempt."""

    operation_id: str
    run_id: int
    run_key: str
    status: PublicDispatchStatus
    job_name: str | None
    launch_count: int
    adopted_only: bool


def validate_provider_event_dispatch_request(
    request: ProviderEventDispatchRequest,
    *,
    ttl_seconds: int,
    now: datetime | None = None,
) -> None:
    """Validate audience, dispatch mode, and request freshness."""

    if request.audience != PROVIDER_EVENT_DISPATCH_AUDIENCE:
        raise ProviderEventDispatchValidationError("invalid_audience")
    if request.dispatch_mode != "offline":
        raise ProviderEventDispatchValidationError("invalid_dispatch_mode")
    current_time = now or datetime.now(timezone.utc)
    issued_at = request.issued_at
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    age_seconds = (current_time - issued_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0 or age_seconds > ttl_seconds:
        raise ProviderEventDispatchValidationError("dispatch_request_expired")


def public_status_for_inbox_state(state: str) -> PublicDispatchStatus:
    """Map durable inbox state to the public dispatch status vocabulary."""

    if state == "launched":
        return "started"
    if state == "terminal":
        return "terminal"
    return "adopted"


def dispatch_snapshot(request: ProviderEventDispatchRequest) -> DispatchInboxSnapshot:
    """Return the authorization snapshot stored with one inbox adoption."""

    return DispatchInboxSnapshot(
        run_key=request.run_key,
        receipt_id=request.receipt_id,
        accepted_activation_revision=request.accepted_activation_revision,
    )


def dispatch_provider_event_offline(
    *,
    inbox: ProviderEventDispatchInbox,
    request: ProviderEventDispatchRequest,
    launch_job: Callable[[ProviderEventDispatchRequest], str | None],
) -> ProviderEventDispatchOutcome:
    """Adopt one dispatch operation, then launch at most one offline job.

    # TODO: Stop requiring a container-local inbox once adoption is recorded
    only through Orchestra downstream adoption.
    """

    snapshot = dispatch_snapshot(request)
    adopted = inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
        snapshot=snapshot,
    )
    if adopted.state in {"launched", "terminal"}:
        return ProviderEventDispatchOutcome(
            operation_id=adopted.operation_id,
            run_id=adopted.run_id,
            run_key=adopted.run_key,
            status=public_status_for_inbox_state(adopted.state),
            job_name=adopted.job_name,
            launch_count=adopted.launch_count,
            adopted_only=True,
        )

    claimed = inbox.claim_launch(operation_id=request.operation_id)
    if not claimed.owns_launch:
        return ProviderEventDispatchOutcome(
            operation_id=claimed.operation_id,
            run_id=claimed.run_id,
            run_key=claimed.run_key,
            status=public_status_for_inbox_state(claimed.state),
            job_name=claimed.job_name,
            launch_count=claimed.launch_count,
            adopted_only=True,
        )

    try:
        job_name = launch_job(request)
    except ProviderEventDispatchValidationError:
        inbox.mark_terminal(
            operation_id=request.operation_id,
            reason="offline_job_launch_failed",
        )
        raise
    except Exception:
        raise

    launched = inbox.launch_if_owner(
        operation_id=request.operation_id,
        job_name=job_name,
    )
    return ProviderEventDispatchOutcome(
        operation_id=launched.operation_id,
        run_id=launched.run_id,
        run_key=launched.run_key,
        status=public_status_for_inbox_state(launched.state),
        job_name=launched.job_name,
        launch_count=launched.launch_count,
        adopted_only=False,
    )


__all__ = [
    "PROVIDER_EVENT_DISPATCH_AUDIENCE",
    "ProviderEventDispatchOutcome",
    "ProviderEventDispatchRequest",
    "ProviderEventDispatchValidationError",
    "PublicDispatchStatus",
    "dispatch_provider_event_offline",
    "dispatch_snapshot",
    "public_status_for_inbox_state",
    "validate_provider_event_dispatch_request",
]
