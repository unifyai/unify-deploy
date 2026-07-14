"""Communication offline provider-event dispatch adoption tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from communication.infra.provider_event_dispatch import (
    ProviderEventDispatchRequest,
    dispatch_provider_event_offline,
    dispatch_snapshot,
)
from communication.infra.provider_event_dispatch_inbox import (
    DispatchInboxSnapshot,
    ProviderEventDispatchInbox,
    ProviderEventInboxMismatchError,
)


def _snapshot(**overrides) -> DispatchInboxSnapshot:
    payload = {
        "run_key": "offline:provider_event:assistant-1:task-1:rev:evt-1",
        "receipt_id": "receipt-1",
        "accepted_activation_revision": "rev-1",
    }
    payload.update(overrides)
    return DispatchInboxSnapshot(**payload)


def _request(**overrides) -> ProviderEventDispatchRequest:
    payload = {
        "operation_id": "op-provider-event-1",
        "run_id": 9001,
        "run_key": "offline:provider_event:assistant-1:task-1:rev:evt-1",
        "assistant_id": "assistant-1",
        "task_id": 101,
        "binding_id": "binding-1",
        "receipt_id": "receipt-1",
        "accepted_activation_revision": "rev-1",
        "event_context_ref": "blob://binding-1/receipt-1",
        "issued_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    return ProviderEventDispatchRequest(**payload)


def test_duplicate_offline_provider_dispatch_adopts_one_inbox_record_and_launches_one_job(
    tmp_path,
) -> None:
    inbox = ProviderEventDispatchInbox(tmp_path / "dispatch-inbox.sqlite3")
    launch_calls: list[str] = []

    def launch_job(request: ProviderEventDispatchRequest) -> str:
        launch_calls.append(request.operation_id)
        return f"unity-task-run-{request.operation_id}"

    request = _request()
    first = dispatch_provider_event_offline(
        inbox=inbox,
        request=request,
        launch_job=launch_job,
    )
    second = dispatch_provider_event_offline(
        inbox=inbox,
        request=request,
        launch_job=launch_job,
    )

    assert first.status == "started"
    assert first.launch_count == 1
    assert second.status == "started"
    assert second.launch_count == 1
    assert second.adopted_only is True
    assert launch_calls == ["op-provider-event-1"]

    adopted = inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
        snapshot=dispatch_snapshot(request),
    )
    assert adopted.state == "launched"
    assert adopted.launch_count == 1
    assert adopted.job_name == "unity-task-run-op-provider-event-1"


def test_concurrent_claim_launch_allows_one_owner(tmp_path) -> None:
    inbox = ProviderEventDispatchInbox(tmp_path / "dispatch-inbox.sqlite3")
    request = _request(operation_id="op-concurrent-claim")
    snapshot = dispatch_snapshot(request)
    inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
        snapshot=snapshot,
    )

    def claim_once() -> bool:
        return inbox.claim_launch(operation_id=request.operation_id).owns_launch

    with ThreadPoolExecutor(max_workers=4) as executor:
        owners = list(executor.map(lambda _: claim_once(), range(4)))

    assert owners.count(True) == 1
    assert owners.count(False) == 3
    record = inbox.get(operation_id=request.operation_id)
    assert record is not None
    assert record.state == "launching"
    assert record.launch_count == 0


def test_launch_if_owner_requires_claimed_state(tmp_path) -> None:
    inbox = ProviderEventDispatchInbox(tmp_path / "dispatch-inbox.sqlite3")
    request = _request(operation_id="op-owner-only")
    snapshot = dispatch_snapshot(request)
    inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
        snapshot=snapshot,
    )

    with pytest.raises(RuntimeError, match="not owned for launch"):
        inbox.launch_if_owner(operation_id=request.operation_id)

    record = inbox.get(operation_id=request.operation_id)
    assert record is not None
    assert record.launch_count == 0
    assert record.state == "adopted"


def test_adopt_or_get_rejects_authorization_mismatch(tmp_path) -> None:
    inbox = ProviderEventDispatchInbox(tmp_path / "dispatch-inbox.sqlite3")
    request = _request(operation_id="op-mismatch")
    inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
        snapshot=dispatch_snapshot(request),
    )

    with pytest.raises(ProviderEventInboxMismatchError):
        inbox.adopt_or_get(
            operation_id=request.operation_id,
            run_id=request.run_id,
            snapshot=_snapshot(
                run_key="offline:provider_event:assistant-1:task-1:rev:other",
            ),
        )
