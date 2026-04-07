from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from communication.infra.assistant_sessions import (
    binding_id as binding_id_from_status,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_ref,
    build_binding,
    session_signal,
    session_signals,
)

BINDING_UNSET = object()


def now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).isoformat()


def binding_payload(
    binding: dict | None,
    *,
    binding_id: str | object = BINDING_UNSET,
    job_ref: dict | None | object = BINDING_UNSET,
    pod_ref: dict | None | object = BINDING_UNSET,
    vm_ref: dict | None | object = BINDING_UNSET,
    desktop_url: str | None | object = BINDING_UNSET,
    created_at: str | None | object = BINDING_UNSET,
    container_bootstrap_started_at: str | None | object = BINDING_UNSET,
    container_ready_at: str | None | object = BINDING_UNSET,
    vm_assigned_at: str | None | object = BINDING_UNSET,
    guest_handshake_started_at: str | None | object = BINDING_UNSET,
    vm_ready_observed_at: str | None | object = BINDING_UNSET,
    vm_ready_hostname: str | None | object = BINDING_UNSET,
    vm_ready_message_id: str | None | object = BINDING_UNSET,
    release_requested_at: str | None | object = BINDING_UNSET,
    release_completed_at: str | None | object = BINDING_UNSET,
) -> dict:
    """Return the canonical binding payload with selective field overrides."""

    binding = binding or {}
    return build_binding(
        binding_id=(
            binding_id_from_status(binding)
            if binding_id is BINDING_UNSET
            else str(binding_id or "")
        ),
        job_ref=(
            binding_job_ref(binding) or None if job_ref is BINDING_UNSET else job_ref
        ),
        pod_ref=(
            binding_pod_ref(binding) or None if pod_ref is BINDING_UNSET else pod_ref
        ),
        vm_ref=binding_vm_ref(binding) or None if vm_ref is BINDING_UNSET else vm_ref,
        desktop_url=(
            binding.get("desktopUrl") if desktop_url is BINDING_UNSET else desktop_url
        ),
        created_at=(
            binding.get("createdAt") if created_at is BINDING_UNSET else created_at
        ),
        container_bootstrap_started_at=(
            binding.get("containerBootstrapStartedAt")
            if container_bootstrap_started_at is BINDING_UNSET
            else container_bootstrap_started_at
        ),
        container_ready_at=(
            binding.get("containerReadyAt")
            if container_ready_at is BINDING_UNSET
            else container_ready_at
        ),
        vm_assigned_at=(
            binding.get("vmAssignedAt")
            if vm_assigned_at is BINDING_UNSET
            else vm_assigned_at
        ),
        guest_handshake_started_at=(
            binding.get("guestHandshakeStartedAt")
            if guest_handshake_started_at is BINDING_UNSET
            else guest_handshake_started_at
        ),
        vm_ready_observed_at=(
            binding.get("vmReadyObservedAt")
            if vm_ready_observed_at is BINDING_UNSET
            else vm_ready_observed_at
        ),
        vm_ready_hostname=(
            binding.get("vmReadyHostname")
            if vm_ready_hostname is BINDING_UNSET
            else vm_ready_hostname
        ),
        vm_ready_message_id=(
            binding.get("vmReadyMessageId")
            if vm_ready_message_id is BINDING_UNSET
            else vm_ready_message_id
        ),
        release_requested_at=(
            binding.get("releaseRequestedAt")
            if release_requested_at is BINDING_UNSET
            else release_requested_at
        ),
        release_completed_at=(
            binding.get("releaseCompletedAt")
            if release_completed_at is BINDING_UNSET
            else release_completed_at
        ),
    )


def mint_binding_payload() -> dict:
    """Create a fresh controller-owned runtime binding."""

    return build_binding(binding_id=uuid.uuid4().hex, created_at=now_iso())


def parse_iso_or_none(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into UTC."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def binding_deadline_exceeded(
    binding: dict | None,
    timestamp_key: str,
    timeout_seconds: float,
) -> bool:
    """Return whether the binding has exceeded a phase deadline."""

    timestamp = parse_iso_or_none(str((binding or {}).get(timestamp_key, "") or ""))
    if timestamp is None:
        return False
    elapsed = (datetime.now(timezone.utc) - timestamp).total_seconds()
    return elapsed > timeout_seconds


def binding_signal_matches(signal: dict | None, binding_id: str) -> bool:
    """Return whether a signal still belongs to the current binding."""

    if not binding_id or not isinstance(signal, dict):
        return False
    return str(signal.get("bindingId", "") or "") == binding_id


def signal_age_seconds(signal: dict | None) -> float | None:
    """Return the age of a binding-scoped signal in seconds."""

    observed_at = parse_iso_or_none(str((signal or {}).get("observedAt", "") or ""))
    if observed_at is None:
        return None
    return (datetime.now(timezone.utc) - observed_at).total_seconds()


def signal_by_name(body: dict, signal_name: str) -> dict:
    """Return one named signal payload from the session body."""

    return session_signal(body, signal_name)


def signals_without(body: dict, *signal_names: str) -> dict:
    """Return the current signal map with the named signals removed."""

    signals = deepcopy(session_signals(body))
    for signal_name in signal_names:
        signals.pop(signal_name, None)
    return signals
