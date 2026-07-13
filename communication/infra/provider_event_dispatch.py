"""Offline provider-event dispatch handler for Communication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from pydantic import BaseModel, Field

from communication.infra.provider_event_dispatch_inbox import (
    ProviderEventDispatchInbox,
)


class ProviderEventDispatchRequest(BaseModel):
    """Internal dispatch authorization for provider-event offline execution."""

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
    audience: str = Field(default="communication:provider-event-dispatch")


@dataclass(frozen=True)
class ProviderEventDispatchOutcome:
    """Result of one provider-event dispatch attempt."""

    operation_id: str
    run_id: int
    inbox_state: str
    launch_count: int
    adopted_only: bool


def dispatch_provider_event_offline(
    *,
    inbox: ProviderEventDispatchInbox,
    request: ProviderEventDispatchRequest,
    launch_job: Callable[[ProviderEventDispatchRequest], None],
) -> ProviderEventDispatchOutcome:
    """Adopt one dispatch operation, then launch at most one offline job."""

    if request.dispatch_mode != "offline":
        raise ValueError("offline dispatch handler requires dispatch_mode=offline")

    adopted = inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
    )
    if adopted.state == "launched":
        return ProviderEventDispatchOutcome(
            operation_id=adopted.operation_id,
            run_id=adopted.run_id,
            inbox_state=adopted.state,
            launch_count=adopted.launch_count,
            adopted_only=True,
        )

    claimed = inbox.claim_launch(operation_id=request.operation_id)
    if claimed.state != "launching":
        return ProviderEventDispatchOutcome(
            operation_id=claimed.operation_id,
            run_id=claimed.run_id,
            inbox_state=claimed.state,
            launch_count=claimed.launch_count,
            adopted_only=True,
        )

    launch_job(request)
    launched = inbox.launch_if_owner(operation_id=request.operation_id)
    return ProviderEventDispatchOutcome(
        operation_id=launched.operation_id,
        run_id=launched.run_id,
        inbox_state=launched.state,
        launch_count=launched.launch_count,
        adopted_only=False,
    )
