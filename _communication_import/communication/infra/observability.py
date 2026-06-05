from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
import uuid
from typing import Any, Iterator

_CURRENT_CAUSAL_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "assistant_session_causal_context",
    default=None,
)

_SIGNAL_CAUSAL_FIELD_NAMES = {
    "operation_id": "operationId",
    "parent_operation_id": "parentOperationId",
    "root_operation_id": "rootOperationId",
    "caller": "caller",
    "parent_caller": "parentCaller",
    "root_caller": "rootCaller",
    "reason": "reason",
}
_LOG_CAUSAL_FIELD_NAMES = tuple(_SIGNAL_CAUSAL_FIELD_NAMES.keys())


def _compact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value not in (None, "", [], {}, ())
    }


def _normalized_causal_context(
    causal_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(causal_context, dict):
        return {}
    return _compact_fields(
        {key: causal_context.get(key) for key in _LOG_CAUSAL_FIELD_NAMES},
    )


def build_causal_context(
    *,
    caller: str,
    parent: dict[str, Any] | None = None,
    operation_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a normalized causal context for cross-component observability.

    The returned payload uses flat snake_case keys so it can be merged directly
    into structured log events. Use ``causal_signal_payload()`` when the same
    context needs to be persisted inside ``status.signals``.
    """

    normalized_parent = _normalized_causal_context(parent)
    current_operation_id = operation_id or uuid.uuid4().hex
    return _compact_fields(
        {
            "operation_id": current_operation_id,
            "parent_operation_id": normalized_parent.get("operation_id"),
            "root_operation_id": (
                normalized_parent.get("root_operation_id")
                or normalized_parent.get("operation_id")
                or current_operation_id
            ),
            "caller": caller,
            "parent_caller": normalized_parent.get("caller"),
            "root_caller": (
                normalized_parent.get("root_caller")
                or normalized_parent.get("caller")
                or caller
            ),
            "reason": reason,
        },
    )


def child_causal_context(
    *,
    caller: str,
    parent: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a child causal context from the current or provided parent."""

    return build_causal_context(
        caller=caller,
        parent=parent or current_causal_context(),
        reason=reason,
    )


def current_causal_context() -> dict[str, Any]:
    """Return the currently bound causal context, if any."""

    return dict(_normalized_causal_context(_CURRENT_CAUSAL_CONTEXT.get()))


def push_causal_context(causal_context: dict[str, Any] | None) -> Token:
    """Bind a causal context until the returned token is reset."""

    return _CURRENT_CAUSAL_CONTEXT.set(
        _normalized_causal_context(causal_context) or None,
    )


def pop_causal_context(token: Token) -> None:
    """Restore the previous causal context after ``push_causal_context()``."""

    _CURRENT_CAUSAL_CONTEXT.reset(token)


@contextmanager
def bind_causal_context(causal_context: dict[str, Any] | None) -> Iterator[None]:
    """Bind a causal context for all nested structured observability events."""

    token = push_causal_context(causal_context)
    try:
        yield
    finally:
        pop_causal_context(token)


def causal_log_fields(
    causal_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return flat log fields for the provided or currently bound context."""

    return _normalized_causal_context(causal_context or current_causal_context())


def causal_signal_payload(
    causal_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a signal-safe causal payload using camelCase keys."""

    normalized = _normalized_causal_context(causal_context or current_causal_context())
    return _compact_fields(
        {
            signal_key: normalized.get(log_key)
            for log_key, signal_key in _SIGNAL_CAUSAL_FIELD_NAMES.items()
        },
    )


def signal_causal_context(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract a normalized causal context from a persisted signal payload."""

    signal_payload = payload.get("causal") if isinstance(payload, dict) else None
    if not isinstance(signal_payload, dict):
        return {}
    return _compact_fields(
        {
            log_key: signal_payload.get(signal_key)
            for log_key, signal_key in _SIGNAL_CAUSAL_FIELD_NAMES.items()
        },
    )
