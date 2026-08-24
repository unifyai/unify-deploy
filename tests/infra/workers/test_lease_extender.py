"""Unit tests for the thread-backed Pub/Sub lease controller."""

from __future__ import annotations

import logging
import signal
import threading
import time

import pytest
from pydantic import BaseModel

from unify.common.pipeline import PipelineHeartbeatManifest
from unify_deploy.infra.workers import worker_utils
from unify_deploy.infra.workers.worker_utils import LeaseController, LeaseExtender


class _StubQueue:
    """Minimal queue that records every sync lease call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def extend_lease_sync(self, receipt_id: str, seconds: int) -> None:
        with self._lock:
            self.calls.append((receipt_id, seconds))


class _FailingQueue:
    """Queue whose sync lease extension always raises."""

    def __init__(self) -> None:
        self.calls: int = 0

    def extend_lease_sync(self, receipt_id: str, seconds: int) -> None:
        self.calls += 1
        raise RuntimeError("pubsub unavailable")


class _StubLedger:
    """Minimal RunLedger capturing every manifest written."""

    def __init__(self) -> None:
        self.writes: list[BaseModel] = []
        self.flushed: int = 0
        self.closed: bool = False

    def write(self, manifest: BaseModel) -> None:
        self.writes.append(manifest)

    def flush(self) -> None:
        self.flushed += 1

    def close(self) -> None:
        self.closed = True


class _FailingLedger:
    """RunLedger whose ``write`` always raises."""

    def __init__(self) -> None:
        self.attempts: int = 0

    def write(self, manifest: BaseModel) -> None:
        self.attempts += 1
        raise RuntimeError("gcs unavailable")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_lease_controller_fires_while_main_thread_is_blocked() -> None:
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-A",
        job_id="job-1",
        period_seconds=0.5,
        extension_seconds=300,
    )

    controller.start()
    time.sleep(5.0)
    controller.stop(outcome="ack")

    assert len(queue.calls) >= 8
    receipt, seconds = queue.calls[0]
    assert receipt == "rcpt-A"
    assert seconds == 300
    assert controller.state == "ACKED"


def test_lease_controller_stops_promptly_mid_sleep() -> None:
    """stop() must not wait for the current sleep window to elapse."""
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-B",
        period_seconds=30.0,
        extension_seconds=300,
    )
    controller.start()
    time.sleep(0.01)

    t0 = time.monotonic()
    controller.stop(outcome="ack")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"stop() should be near-instant, took {elapsed}s"
    assert queue.calls == [], "no extensions should have fired in 10ms window"


def test_lease_controller_logs_critical_after_repeated_failures(caplog) -> None:
    queue = _FailingQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-C",
        period_seconds=0.03,
        extension_seconds=300,
        max_consecutive_failures=2,
    )

    with caplog.at_level(logging.CRITICAL):
        controller.start()
        time.sleep(0.12)
        controller.stop(outcome="error")

    assert queue.calls >= 2
    assert controller.state == "FAILED"
    assert "Lease extension failed" in caplog.text


def test_lease_controller_stop_nack_modifies_deadline_zero_once() -> None:
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-N",
        period_seconds=30.0,
        extension_seconds=300,
    )

    controller.start()
    controller.stop(outcome="nack")

    assert queue.calls == [("rcpt-N", 0)]
    assert controller.state == "NACKED"


@pytest.mark.asyncio
async def test_signal_handler_sets_shutdown_without_nacking(monkeypatch) -> None:
    handlers = {}

    class _Loop:
        def add_signal_handler(self, sig, callback, *args):
            handlers[sig] = (callback, args)

    monkeypatch.setattr(worker_utils.asyncio, "get_running_loop", lambda: _Loop())

    def fail_nack(*_args, **_kwargs):
        raise AssertionError("SIGTERM must not nack active receipts")

    monkeypatch.setattr(worker_utils, "nack_active_leases", fail_nack)

    worker_utils.install_signal_handlers()
    callback, args = handlers[signal.SIGTERM]
    callback(*args)

    assert worker_utils.is_shutdown_requested() is True


def test_lease_controller_start_is_idempotent_vs_stop() -> None:
    """Calling stop() without start() or twice is safe (no hang / crash)."""
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-D",
        period_seconds=1.0,
        extension_seconds=300,
    )
    # stop() before start() must not raise or hang.
    controller.stop(outcome="ack")

    controller.start()
    time.sleep(0.01)
    controller.stop(outcome="ack")
    # Second stop() on an already-stopped extender must be idempotent.
    controller.stop(outcome="ack")


def test_lease_controller_writes_heartbeat_per_tick() -> None:
    """With a run_ledger attached, each successful extension writes a
    ``PipelineHeartbeatManifest`` tagged with the run + stage so ops
    can query ``max(now - last_progress_at)`` across active runs."""
    queue = _StubQueue()
    ledger = _StubLedger()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-HB",
        job_id="job-heartbeat",
        period_seconds=0.05,
        extension_seconds=300,
        run_ledger=ledger,  # type: ignore[arg-type]
        run_id="run-heartbeat",
        stage="ingest",
    )

    controller.start()
    time.sleep(0.17)  # Expect ~3 ticks.
    controller.stop(outcome="ack")

    # One heartbeat per successful extension — counts must match.
    assert len(queue.calls) >= 2, "expected periodic extensions"
    assert len(ledger.writes) == len(queue.calls), (
        "heartbeat count must track extension count "
        f"(extensions={len(queue.calls)}, heartbeats={len(ledger.writes)})"
    )

    # Every persisted record carries the right identity + stage.
    for idx, manifest in enumerate(ledger.writes, start=1):
        assert isinstance(manifest, PipelineHeartbeatManifest)
        assert manifest.run_id == "run-heartbeat"
        assert manifest.stage == "ingest"
        assert manifest.receipt_id != "rcpt-HB"
        assert manifest.job_id == "job-heartbeat"
        # extensions_emitted counts from 1 upward (heartbeat is written
        # AFTER the extension is recorded, so the nth heartbeat sees n).
        assert manifest.extensions_emitted == idx
        assert manifest.elapsed_seconds >= 0.0


def test_lease_extender_alias_without_ledger_is_backward_compatible() -> None:
    """Without a run_ledger, the extender behaves exactly as before:
    it extends leases, never constructs a heartbeat manifest, and
    never raises for missing ``run_id`` / ``stage``."""
    queue = _StubQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-legacy",
        period_seconds=0.04,
        extension_seconds=300,
    )

    extender.start()
    time.sleep(0.12)
    extender.stop(outcome="ack")

    assert len(queue.calls) >= 2, "legacy path must still extend leases"


def test_lease_extender_requires_run_id_and_stage_with_ledger() -> None:
    """Passing a ledger without run_id / stage is a wiring bug: refuse
    loudly rather than silently dropping heartbeats."""
    queue = _StubQueue()
    ledger = _StubLedger()

    with pytest.raises(ValueError, match="run_id and stage"):
        LeaseController(
            work_queue=queue,  # type: ignore[arg-type]
            receipt_id="rcpt-bad",
            run_ledger=ledger,  # type: ignore[arg-type]
            # run_id + stage deliberately omitted.
        )


def test_lease_controller_heartbeat_failures_do_not_stop_extension() -> None:
    """A ledger-write failure (e.g. transient GCS outage) must never
    prevent the next Pub/Sub lease extension from firing. The Pub/Sub
    lease is the load-bearing invariant; heartbeats are observational."""
    queue = _StubQueue()
    ledger = _FailingLedger()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-hb-fail",
        period_seconds=0.04,
        extension_seconds=300,
        run_ledger=ledger,  # type: ignore[arg-type]
        run_id="run-hb-fail",
        stage="parse",
    )

    controller.start()
    time.sleep(0.17)
    controller.stop(outcome="ack")

    # Extensions and heartbeat attempts both kept firing across failures.
    assert len(queue.calls) >= 2, (
        "lease extensions must continue despite heartbeat ledger "
        f"failures (got only {len(queue.calls)})"
    )
    assert ledger.attempts >= 2, (
        "heartbeat writes must keep being attempted after transient "
        f"failures (got only {ledger.attempts})"
    )
    # Ledger attempts track extension calls 1:1 — a ledger failure
    # does not cause the extender to skip future heartbeats.
    assert ledger.attempts == len(queue.calls)


def test_lease_controller_surrenders_without_nacking_mid_chunk() -> None:
    """Past ``max_lifetime_s`` the controller flags surrender and keeps the
    deadline alive, so the body can reach a chunk boundary and release the
    attempt lease before the message becomes reclaimable.

    Nacking the instant the cap tripped was the defect: the successor arrived
    while the predecessor still held the lease, raised DuplicateLiveAttempt,
    and burned a delivery attempt. With ~50s chunks that fired on every file
    longer than the cap until the message dead-lettered -- while the original
    was still committing rows.
    """
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-cap",
        job_id="job-cap",
        period_seconds=0.05,
        extension_seconds=300,
        max_lifetime_s=0.12,
        surrender_grace_s=30.0,
    )

    controller.start()
    time.sleep(0.6)

    assert controller.surrendered is True
    # Still renewing: the body has not surrendered yet, so the message must
    # stay held rather than becoming reclaimable underneath it.
    assert any(seconds == 300 for _, seconds in queue.calls)
    assert 0 not in [seconds for _, seconds in queue.calls]
    controller.stop(outcome="nack")


def test_lease_controller_nacks_once_the_surrender_grace_is_exhausted() -> None:
    """A body that never reaches a boundary must not hold the message forever.

    This is the backstop the cap exists for, kept as a last resort rather than
    the ordinary path.
    """
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-wedged",
        job_id="job-wedged",
        period_seconds=0.05,
        extension_seconds=300,
        max_lifetime_s=0.1,
        surrender_grace_s=0.2,
    )

    controller.start()
    time.sleep(0.8)

    assert controller.surrendered is True
    assert queue.calls[-1] == ("rcpt-wedged", 0)
    controller.stop(outcome="nack")


def test_the_ordinary_surrender_path_nacks_from_the_body_not_the_controller() -> None:
    """With a grace longer than the run, the controller never nacks at all.

    The body's own surrender raises RetryWorkItem at a chunk boundary and the
    entrypoint turns that into the redelivery. The controller's job is only to
    keep the message held until then.
    """
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-clean",
        job_id="job-clean",
        period_seconds=0.05,
        extension_seconds=300,
        max_lifetime_s=0.1,
        surrender_grace_s=60.0,
    )

    controller.start()
    time.sleep(0.5)
    # Stopping with "ack" stands in for the body finishing its unwind: the
    # entrypoint owns the nack, so the controller must not have issued one.
    controller.stop(outcome="ack")

    assert controller.surrendered is True
    assert all(seconds != 0 for _, seconds in queue.calls)


def test_lease_controller_surrenders_after_max_extensions() -> None:
    """``max_extensions`` bounds the renewals before surrender is flagged.

    It does not bound the renewals during the drain: holding the message is
    the whole point of the drain, so extensions continue until the body
    unwinds or the grace runs out.
    """
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-ext-cap",
        job_id="job-ext-cap",
        period_seconds=0.04,
        extension_seconds=300,
        max_extensions=2,
        surrender_grace_s=30.0,
    )

    controller.start()
    time.sleep(0.4)

    assert controller.surrendered is True
    assert controller.extensions >= 2
    assert all(seconds == 300 for _, seconds in queue.calls)
    controller.stop(outcome="nack")


def test_lease_controller_no_cap_does_not_surrender() -> None:
    """Without a cap the controller keeps extending and never surrenders
    (preserves the prior unbounded behavior for opt-out callers)."""
    queue = _StubQueue()
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-nocap",
        period_seconds=0.04,
        extension_seconds=300,
    )

    controller.start()
    time.sleep(0.2)
    assert controller.surrendered is False
    assert len(queue.calls) >= 2
    assert all(seconds == 300 for _, seconds in queue.calls)
    controller.stop(outcome="ack")


def test_lease_controller_logs_only_receipt_hash(caplog) -> None:
    queue = _StubQueue()
    raw_receipt = "raw-receipt-secret"
    controller = LeaseController(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id=raw_receipt,
        job_id="job-hash",
        period_seconds=0.03,
        extension_seconds=300,
    )

    with caplog.at_level(logging.INFO):
        controller.start()
        time.sleep(0.08)
        controller.stop(outcome="ack")

    assert queue.calls
    assert raw_receipt not in caplog.text
    assert "receipt_hash=" in caplog.text
