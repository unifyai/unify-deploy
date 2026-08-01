"""Unit tests for the dispatch-time overlap skip.

A due scheduled occurrence whose predecessor is still genuinely running
terminalizes at dispatch — one run lookup and one Job status read —
instead of booting a worker pod to discover the overlap. Explicit,
triggered, and provider-event wakes are content-bearing and are never
swallowed by the check.
"""

from __future__ import annotations

import pytest

from communication.infra import task_execution
from communication.infra.models import OfflineTaskDispatchRequest


def _request(wake: str = "scheduled") -> OfflineTaskDispatchRequest:
    return OfflineTaskDispatchRequest(
        assistant_id="1406",
        task_id=12,
        source_task_log_id=555,
        revision="rev-1",
        wake=wake,
        scheduled_for="2026-08-01T10:00:00+00:00",
    )


@pytest.fixture
def running_sibling(monkeypatch):
    """Pin a predecessor run that is in flight with an active Job."""

    monkeypatch.setattr(
        task_execution,
        "_lookup_latest_task_run",
        lambda **kwargs: {
            "run_key": "offline:scheduled:1406:12:rev:0930",
            "state": "running",
            "job_name": "unity-task-execution-abc",
        },
    )
    monkeypatch.setattr(
        task_execution,
        "_classify_offline_job_status",
        lambda batch_api, job_name: {"status": "active"},
    )


def test_scheduled_overlap_terminalizes_without_boot(monkeypatch, running_sibling):
    adopted: list[dict] = []
    updates: list[dict] = []
    monkeypatch.setattr(
        task_execution,
        "_create_or_adopt_task_run",
        lambda payload: adopted.append(payload) or {"run": payload, "created": False},
    )
    monkeypatch.setattr(
        task_execution,
        "_update_task_run",
        lambda **kwargs: updates.append(kwargs) or {"run": {}},
    )

    result = task_execution._skip_overlapped_scheduled_occurrence(
        batch_api=object(),
        request=_request(),
        run_key="offline:scheduled:1406:12:rev:1000",
        execution={"run_key": "offline:scheduled:1406:12:rev:1000"},
    )

    assert result is not None and result["status"] == "skipped_overlap"
    assert result["predecessor_run_key"] == "offline:scheduled:1406:12:rev:0930"
    assert len(adopted) == 1, "the occurrence row must be adopted before terminalizing"
    assert len(updates) == 1
    terminal = updates[0]["updates"]
    assert terminal["state"] == "completed"
    assert "overlap_skip" in terminal["result_summary"]


@pytest.mark.parametrize("wake", ["explicit", "triggered", "provider_event"])
def test_content_bearing_wakes_are_never_swallowed(monkeypatch, running_sibling, wake):
    result = task_execution._skip_overlapped_scheduled_occurrence(
        batch_api=object(),
        request=_request(wake=wake),
        run_key="k-new",
        execution=None,
    )
    assert result is None


def test_no_skip_without_a_genuinely_active_job(monkeypatch):
    monkeypatch.setattr(
        task_execution,
        "_lookup_latest_task_run",
        lambda **kwargs: {
            "run_key": "k-old",
            "state": "running",
            "job_name": "unity-task-execution-gone",
        },
    )
    # The recorded Job vanished: this is the stale-inflight repair path's
    # problem, not an overlap.
    monkeypatch.setattr(
        task_execution,
        "_classify_offline_job_status",
        lambda batch_api, job_name: {"status": "missing"},
    )

    result = task_execution._skip_overlapped_scheduled_occurrence(
        batch_api=object(),
        request=_request(),
        run_key="k-new",
        execution=None,
    )
    assert result is None


def test_no_skip_when_the_latest_run_is_this_occurrence(monkeypatch):
    monkeypatch.setattr(
        task_execution,
        "_lookup_latest_task_run",
        lambda **kwargs: {
            "run_key": "k-new",
            "state": "running",
            "job_name": "unity-task-execution-self",
        },
    )

    result = task_execution._skip_overlapped_scheduled_occurrence(
        batch_api=object(),
        request=_request(),
        run_key="k-new",
        execution=None,
    )
    assert result is None


class TestDispatchRunKeyResolution:
    """A scheduled dispatch names its run from the ledger, not from parts."""

    def _execution(self, run_key: str) -> dict:
        return {"run_key": run_key, "task_id": 12, "wake": "scheduled"}

    def test_scheduled_adopts_the_projected_key(self, monkeypatch):
        monkeypatch.setattr(
            task_execution,
            "_build_offline_run_key",
            lambda request: "rebuilt-and-wrong",
        )
        resolved = task_execution._resolve_offline_dispatch_run_key(
            _request(),
            self._execution("offline:scheduled:1406:team-11:12:abc:20260801T100000Z"),
        )
        assert resolved == "offline:scheduled:1406:team-11:12:abc:20260801T100000Z", (
            "the scheduled lane rebuilt a key it was already holding; any "
            "normalisation drift between the two builders mints a twin"
        )

    @pytest.mark.parametrize("wake", ["triggered", "explicit", "provider_event"])
    def test_other_lanes_still_construct(self, monkeypatch, wake):
        monkeypatch.setattr(
            task_execution,
            "_build_offline_run_key",
            lambda request: "constructed",
        )
        resolved = task_execution._resolve_offline_dispatch_run_key(
            _request(wake=wake),
            self._execution("stored-but-not-mine"),
        )
        assert resolved == "constructed"

    def test_scheduled_falls_back_when_the_row_has_no_key(self, monkeypatch):
        monkeypatch.setattr(
            task_execution,
            "_build_offline_run_key",
            lambda request: "constructed",
        )
        assert (
            task_execution._resolve_offline_dispatch_run_key(_request(), {})
            == "constructed"
        )
        assert (
            task_execution._resolve_offline_dispatch_run_key(_request(), None)
            == "constructed"
        )
