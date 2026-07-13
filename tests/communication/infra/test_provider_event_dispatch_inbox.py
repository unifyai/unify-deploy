"""Communication offline provider-event dispatch adoption tests."""

from __future__ import annotations

from datetime import datetime, timezone

from communication.infra.provider_event_dispatch_inbox import ProviderEventDispatchInbox
from communication.infra.provider_event_dispatch import (
    ProviderEventDispatchRequest,
    dispatch_provider_event_offline,
)


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

    def launch_job(request: ProviderEventDispatchRequest) -> None:
        launch_calls.append(request.operation_id)

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

    assert first.launch_count == 1
    assert second.launch_count == 1
    assert second.adopted_only is True
    assert launch_calls == ["op-provider-event-1"]

    adopted = inbox.adopt_or_get(
        operation_id=request.operation_id,
        run_id=request.run_id,
    )
    assert adopted.state == "launched"
    assert adopted.launch_count == 1
