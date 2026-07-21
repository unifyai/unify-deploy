"""Offline provider-event dispatch helpers for Communication."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field

from common.settings import SETTINGS

PROVIDER_EVENT_DISPATCH_AUDIENCE = "communication:provider-event-dispatch"
PublicDispatchStatus = Literal["adopted", "started", "terminal"]

ORCHESTRA_DISPATCH_CLAIM_PATH = "/admin/provider-event-dispatch/claim"
ORCHESTRA_DISPATCH_REPORT_STARTED_PATH = "/admin/provider-event-dispatch/report-started"
ORCHESTRA_DISPATCH_REPORT_TERMINAL_PATH = (
    "/admin/provider-event-dispatch/report-terminal"
)
_HTTP_TIMEOUT_SECONDS = 30


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
    accepted_revision: str
    wake: Literal["provider_event"] = "provider_event"
    delivery: Literal["live", "offline"] = "offline"
    event_context_ref: str
    issued_at: datetime
    audience: str = Field(default=PROVIDER_EVENT_DISPATCH_AUDIENCE)


class ProviderEventDispatchValidationError(ValueError):
    """Raised when one dispatch request fails authorization validation."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ProviderEventDispatchAuthorizationError(ValueError):
    """Raised when Orchestra rejects a reused operation authorization snapshot."""

    def __init__(self, reason_code: str = "dispatch_authorization_mismatch") -> None:
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
    fencing_token: int
    adopted_only: bool
    terminal_reason: str | None = None


def validate_provider_event_dispatch_request(
    request: ProviderEventDispatchRequest,
    *,
    ttl_seconds: int,
    now: datetime | None = None,
) -> None:
    """Validate audience, dispatch mode, and request freshness."""

    if request.audience != PROVIDER_EVENT_DISPATCH_AUDIENCE:
        raise ProviderEventDispatchValidationError("invalid_audience")
    if request.delivery != "offline":
        raise ProviderEventDispatchValidationError("invalid_delivery")
    current_time = now or datetime.now(timezone.utc)
    issued_at = request.issued_at
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    age_seconds = (current_time - issued_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0 or age_seconds > ttl_seconds:
        raise ProviderEventDispatchValidationError("dispatch_request_expired")


def offline_launch_identity(*, run_key: str, job_name: str) -> str:
    """Return the deterministic offline sink identity for one operation."""

    return job_name or f"unity-task-execution:{run_key}"


def _orchestra_admin_headers() -> dict[str, str]:
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY") or SETTINGS.orchestra_admin_key
    if not admin_key:
        raise RuntimeError("ORCHESTRA_ADMIN_KEY must be configured")
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def _orchestra_admin_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not SETTINGS.orchestra_url:
        raise RuntimeError("ORCHESTRA_URL must be configured")
    response = requests.post(
        f"{SETTINGS.orchestra_url}{path}",
        json=payload,
        headers=_orchestra_admin_headers(),
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 409:
        detail = response.json() if response.content else {}
        reason = "dispatch_authorization_mismatch"
        if isinstance(detail, dict):
            nested = detail.get("detail")
            if isinstance(nested, dict) and nested.get("reason"):
                reason = str(nested["reason"])
            elif detail.get("reason"):
                reason = str(detail["reason"])
        raise ProviderEventDispatchAuthorizationError(reason)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Orchestra response for {path}")
    return body


def _claimant_id() -> str:
    hostname = os.environ.get("HOSTNAME") or uuid.uuid4().hex[:8]
    return f"communication:{hostname}:{os.getpid()}"


def claim_provider_event_dispatch(
    request: ProviderEventDispatchRequest,
    *,
    launch_identity: str,
    claimant_id: str | None = None,
) -> ProviderEventDispatchOutcome:
    """Claim Orchestra launch ownership before offline job I/O."""

    body = _orchestra_admin_post(
        ORCHESTRA_DISPATCH_CLAIM_PATH,
        {
            "operation_id": request.operation_id,
            "run_id": request.run_id,
            "run_key": request.run_key,
            "assistant_id": request.assistant_id,
            "task_id": request.task_id,
            "binding_id": request.binding_id,
            "receipt_id": request.receipt_id,
            "accepted_revision": request.accepted_revision,
            "delivery": request.delivery,
            "audience": request.audience,
            "claimant_id": claimant_id or _claimant_id(),
            "launch_identity": launch_identity,
        },
    )
    return ProviderEventDispatchOutcome(
        operation_id=str(body["operation_id"]),
        run_id=int(body["run_id"]),
        run_key=str(body["run_key"]),
        status=str(body["status"]),  # type: ignore[arg-type]
        job_name=body.get("launch_identity"),
        fencing_token=int(body["fencing_token"]),
        adopted_only=not bool(body.get("owns_launch")),
        terminal_reason=body.get("terminal_reason"),
    )


def report_provider_event_dispatch_started(
    *,
    operation_id: str,
    fencing_token: int,
    launch_identity: str | None,
) -> ProviderEventDispatchOutcome:
    """Report fenced start after the deterministic offline job is reconciled."""

    body = _orchestra_admin_post(
        ORCHESTRA_DISPATCH_REPORT_STARTED_PATH,
        {
            "operation_id": operation_id,
            "fencing_token": fencing_token,
            "launch_identity": launch_identity,
        },
    )
    return ProviderEventDispatchOutcome(
        operation_id=str(body["operation_id"]),
        run_id=int(body["run_id"]),
        run_key=str(body["run_key"]),
        status=str(body["status"]),  # type: ignore[arg-type]
        job_name=body.get("launch_identity"),
        fencing_token=int(body["fencing_token"]),
        adopted_only=False,
        terminal_reason=body.get("terminal_reason"),
    )


def report_provider_event_dispatch_terminal(
    *,
    operation_id: str,
    fencing_token: int,
    terminal_reason: str,
    launch_identity: str | None = None,
) -> ProviderEventDispatchOutcome:
    """Report fenced terminal failure for one offline dispatch attempt."""

    body = _orchestra_admin_post(
        ORCHESTRA_DISPATCH_REPORT_TERMINAL_PATH,
        {
            "operation_id": operation_id,
            "fencing_token": fencing_token,
            "terminal_reason": terminal_reason,
            "launch_identity": launch_identity,
        },
    )
    return ProviderEventDispatchOutcome(
        operation_id=str(body["operation_id"]),
        run_id=int(body["run_id"]),
        run_key=str(body["run_key"]),
        status=str(body["status"]),  # type: ignore[arg-type]
        job_name=body.get("launch_identity"),
        fencing_token=int(body["fencing_token"]),
        adopted_only=False,
        terminal_reason=body.get("terminal_reason"),
    )


def dispatch_provider_event_offline(
    *,
    request: ProviderEventDispatchRequest,
    launch_job: Callable[[ProviderEventDispatchRequest], str | None],
    resolve_launch_identity: Callable[[ProviderEventDispatchRequest], str],
) -> ProviderEventDispatchOutcome:
    """Claim through Orchestra, then launch at most one offline job."""

    launch_identity = resolve_launch_identity(request)
    claimed = claim_provider_event_dispatch(
        request,
        launch_identity=launch_identity,
    )
    if claimed.status in {"started", "terminal"} or claimed.adopted_only:
        return claimed

    try:
        job_name = launch_job(request) or launch_identity
    except ProviderEventDispatchValidationError:
        report_provider_event_dispatch_terminal(
            operation_id=request.operation_id,
            fencing_token=claimed.fencing_token,
            terminal_reason="offline_job_launch_failed",
            launch_identity=launch_identity,
        )
        raise

    return report_provider_event_dispatch_started(
        operation_id=request.operation_id,
        fencing_token=claimed.fencing_token,
        launch_identity=job_name,
    )


__all__ = [
    "PROVIDER_EVENT_DISPATCH_AUDIENCE",
    "ProviderEventDispatchAuthorizationError",
    "ProviderEventDispatchOutcome",
    "ProviderEventDispatchRequest",
    "ProviderEventDispatchValidationError",
    "PublicDispatchStatus",
    "claim_provider_event_dispatch",
    "dispatch_provider_event_offline",
    "offline_launch_identity",
    "report_provider_event_dispatch_started",
    "report_provider_event_dispatch_terminal",
    "validate_provider_event_dispatch_request",
]
