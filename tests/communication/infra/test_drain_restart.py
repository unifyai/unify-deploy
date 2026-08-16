"""Tests for assistant drain / restart control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from communication.infra import drain as drain_mod
from communication.infra.drain import (
    DrainIntent,
    RestartRequest,
    admission_blocked,
    assistants_for_bundle,
    request_bundle_drain,
)


def _intent(**overrides) -> DrainIntent:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = {
        "assistant_id": "1406",
        "mode": "graceful",
        "reason": "manual",
        "target_revision": "abc",
        "requested_at": now.isoformat().replace("+00:00", "Z"),
        "deadline_at": now.isoformat().replace("+00:00", "Z"),
        "state": "draining",
        "block_admission": True,
    }
    base.update(overrides)
    return DrainIntent.model_validate(base)


def test_admission_blocked_respects_block_flag():
    with patch.object(drain_mod, "get_drain_intent", return_value=_intent()):
        assert admission_blocked("1406") is not None
    with patch.object(
        drain_mod,
        "get_drain_intent",
        return_value=_intent(block_admission=False, state="done"),
    ):
        assert admission_blocked("1406") is None
    with patch.object(
        drain_mod,
        "get_drain_intent",
        return_value=_intent(state="failed", block_admission=True),
    ):
        assert admission_blocked("1406") is None


def test_assistants_for_bundle_unify_company():
    ids = assistants_for_bundle(bundle_key="unify_company", environment="production")
    assert "1406" in ids
    ids_staging = assistants_for_bundle(
        bundle_key="unify_company",
        environment="staging",
    )
    assert "8081" in ids_staging


@pytest.mark.asyncio
async def test_request_restart_arms_graceful_and_starts_waiter():
    stored: dict[str, DrainIntent] = {}

    def _set(intent: DrainIntent) -> DrainIntent:
        stored[intent.assistant_id] = intent
        return intent

    def _get(aid):
        return stored.get(str(aid))

    with (
        patch.object(drain_mod, "get_drain_intent", side_effect=_get),
        patch.object(drain_mod, "set_drain_intent", side_effect=_set),
        patch.object(drain_mod, "_ensure_waiter", new_callable=AsyncMock) as ensure,
        patch.object(drain_mod, "_stop_live_session", new_callable=AsyncMock),
    ):
        result = await drain_mod.request_restart(
            "1406",
            RestartRequest(mode="graceful", reason="deploy", target_revision="sha1"),
        )
    assert result["status"] == "graceful_armed"
    assert stored["1406"].block_admission is True
    assert stored["1406"].target_revision == "sha1"
    ensure.assert_awaited()


@pytest.mark.asyncio
async def test_request_restart_same_revision_rearms_waiter():
    """A lost waiter is revived by re-requesting the drain, not left blocking."""
    existing = _intent(target_revision="sha1")
    with (
        patch.object(drain_mod, "get_drain_intent", return_value=existing),
        patch.object(drain_mod, "set_drain_intent", side_effect=lambda i: i),
        patch.object(drain_mod, "_ensure_waiter", new_callable=AsyncMock) as ensure,
        patch.object(drain_mod, "_stop_live_session", new_callable=AsyncMock),
    ):
        result = await drain_mod.request_restart(
            "1406",
            RestartRequest(mode="graceful", reason="deploy", target_revision="sha1"),
        )
    assert result["status"] == "already_draining"
    ensure.assert_awaited_once_with("1406")


@pytest.mark.asyncio
async def test_request_restart_force_stops_session():
    stored: dict[str, DrainIntent] = {}

    def _set(intent: DrainIntent) -> DrainIntent:
        stored[intent.assistant_id] = intent
        return intent

    with (
        patch.object(drain_mod, "get_drain_intent", return_value=None),
        patch.object(drain_mod, "set_drain_intent", side_effect=_set),
        patch.object(drain_mod, "_ensure_waiter", new_callable=AsyncMock),
        patch.object(
            drain_mod,
            "_stop_live_session",
            new_callable=AsyncMock,
            return_value={"stopped": True},
        ) as stop,
    ):
        result = await drain_mod.request_restart(
            "1406",
            RestartRequest(mode="force", reason="manual"),
        )
    assert result["status"] == "force_armed"
    stop.assert_awaited()


@pytest.mark.asyncio
async def test_request_bundle_drain_fans_out():
    with (
        patch.object(
            drain_mod,
            "assistants_for_bundle",
            return_value=["1406", "9999"],
        ),
        patch.object(
            drain_mod,
            "request_restart",
            new_callable=AsyncMock,
            return_value={"success": True, "status": "graceful_armed"},
        ) as restart,
    ):
        out = await request_bundle_drain(
            drain_mod.BundleDrainRequest(
                bundle_key="unify_company",
                environment="production",
                target_revision="deadbeef",
            ),
        )
    assert out["assistants"] == ["1406", "9999"]
    assert restart.await_count == 2


@pytest.mark.asyncio
async def test_offline_dispatch_raises_503_when_draining():
    from communication.infra.models import OfflineTaskDispatchRequest
    from communication.infra.task_execution import dispatch_offline_task
    from unify.task_scheduler.types.execution import Wake

    body = OfflineTaskDispatchRequest(
        assistant_id="1406",
        task_id=1,
        source_task_log_id=1,
        revision="r1",
        destination="default",
        delivery="offline",
        wake=Wake.scheduled,
        scheduled_for=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    with (
        patch(
            "communication.infra.task_execution.authorize_admin_or_assistant",
            new_callable=AsyncMock,
        ),
        patch(
            "communication.infra.task_execution._validate_offline_dispatch_request",
        ),
        patch(
            "communication.infra.drain.admission_blocked",
            return_value=_intent(),
        ),
        patch(
            "communication.infra.drain._ensure_waiter",
            new_callable=AsyncMock,
        ) as ensure,
    ):
        with pytest.raises(HTTPException) as exc:
            await dispatch_offline_task(body, MagicMock())
    assert exc.value.status_code == 503
    # A deferred dispatch revives the drain waiter, so a lost one cannot leave
    # admission blocked forever.
    ensure.assert_awaited_once_with("1406")
