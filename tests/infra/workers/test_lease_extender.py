"""Unit tests for ``unity_deploy.infra.workers.worker_utils.LeaseExtender``.

Validates that the lease extender:

* Calls ``work_queue.extend_lease`` periodically with the configured
  ``extension_seconds`` value while running.
* Stops promptly when :meth:`LeaseExtender.stop` is called, even if
  the current sleep window has most of its duration remaining.
* Survives transient ``extend_lease`` failures (logs the error, keeps
  the background task alive, retries on the next period).
* When a ``run_ledger`` + ``run_id`` + ``stage`` are supplied, writes
  one ``PipelineHeartbeatManifest`` per successful extension tick, so
  ops can monitor ``max(now - last_progress_at)`` across active runs
  to detect hung pods.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from unity.common.pipeline import PipelineHeartbeatManifest
from unity_deploy.infra.workers.worker_utils import LeaseExtender


class _StubQueue:
    """Minimal queue that records every ``extend_lease`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def extend_lease(self, receipt_id: str, seconds: int) -> None:
        self.calls.append((receipt_id, seconds))


class _FailingQueue:
    """Queue whose ``extend_lease`` always raises."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def extend_lease(self, receipt_id: str, seconds: int) -> None:
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


@pytest.mark.asyncio
async def test_lease_extender_fires_periodically() -> None:
    queue = _StubQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-A",
        job_id="job-1",
        period_seconds=0.05,
        extension_seconds=600,
    )

    extender.start()
    await asyncio.sleep(0.17)  # Expect ~3 periods to elapse.
    await extender.stop()

    assert (
        len(queue.calls) >= 2
    ), f"expected at least 2 extensions, got {len(queue.calls)}"
    receipt, seconds = queue.calls[0]
    assert receipt == "rcpt-A"
    assert seconds == 600


@pytest.mark.asyncio
async def test_lease_extender_stops_promptly_mid_sleep() -> None:
    """stop() must not wait for the current sleep window to elapse."""
    queue = _StubQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-B",
        period_seconds=30.0,
        extension_seconds=600,
    )
    extender.start()
    await asyncio.sleep(0.01)

    t0 = time.monotonic()
    await extender.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"stop() should be near-instant, took {elapsed}s"
    assert queue.calls == [], "no extensions should have fired in 10ms window"


@pytest.mark.asyncio
async def test_lease_extender_survives_transient_failures() -> None:
    queue = _FailingQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-C",
        period_seconds=0.03,
        extension_seconds=600,
    )

    extender.start()
    await asyncio.sleep(0.12)
    await extender.stop()

    assert queue.calls >= 2, (
        "extender must retry across failures; "
        f"got only {queue.calls} attempted extensions"
    )


@pytest.mark.asyncio
async def test_lease_extender_start_is_idempotent_vs_stop() -> None:
    """Calling stop() without start() or twice is safe (no hang / crash)."""
    queue = _StubQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-D",
        period_seconds=1.0,
        extension_seconds=600,
    )
    # stop() before start() must not raise or hang.
    await extender.stop()

    extender.start()
    await asyncio.sleep(0.01)
    await extender.stop()
    # Second stop() on an already-stopped extender must be idempotent.
    await extender.stop()


@pytest.mark.asyncio
async def test_lease_extender_writes_heartbeat_per_tick() -> None:
    """With a run_ledger attached, each successful extension writes a
    ``PipelineHeartbeatManifest`` tagged with the run + stage so ops
    can query ``max(now - last_progress_at)`` across active runs."""
    queue = _StubQueue()
    ledger = _StubLedger()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-HB",
        job_id="job-heartbeat",
        period_seconds=0.05,
        extension_seconds=600,
        run_ledger=ledger,  # type: ignore[arg-type]
        run_id="run-heartbeat",
        stage="ingest",
    )

    extender.start()
    await asyncio.sleep(0.17)  # Expect ~3 ticks.
    await extender.stop()

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
        assert manifest.receipt_id == "rcpt-HB"
        assert manifest.job_id == "job-heartbeat"
        # extensions_emitted counts from 1 upward (heartbeat is written
        # AFTER the extension is recorded, so the nth heartbeat sees n).
        assert manifest.extensions_emitted == idx
        assert manifest.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_lease_extender_without_ledger_is_backward_compatible() -> None:
    """Without a run_ledger, the extender behaves exactly as before:
    it extends leases, never constructs a heartbeat manifest, and
    never raises for missing ``run_id`` / ``stage``."""
    queue = _StubQueue()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-legacy",
        period_seconds=0.04,
        extension_seconds=600,
    )

    extender.start()
    await asyncio.sleep(0.12)
    await extender.stop()

    assert len(queue.calls) >= 2, "legacy path must still extend leases"


def test_lease_extender_requires_run_id_and_stage_with_ledger() -> None:
    """Passing a ledger without run_id / stage is a wiring bug: refuse
    loudly rather than silently dropping heartbeats."""
    queue = _StubQueue()
    ledger = _StubLedger()

    with pytest.raises(ValueError, match="run_id and stage"):
        LeaseExtender(
            work_queue=queue,  # type: ignore[arg-type]
            receipt_id="rcpt-bad",
            run_ledger=ledger,  # type: ignore[arg-type]
            # run_id + stage deliberately omitted.
        )


@pytest.mark.asyncio
async def test_lease_extender_heartbeat_failures_do_not_stop_extension() -> None:
    """A ledger-write failure (e.g. transient GCS outage) must never
    prevent the next Pub/Sub lease extension from firing. The Pub/Sub
    lease is the load-bearing invariant; heartbeats are observational."""
    queue = _StubQueue()
    ledger = _FailingLedger()
    extender = LeaseExtender(
        work_queue=queue,  # type: ignore[arg-type]
        receipt_id="rcpt-hb-fail",
        period_seconds=0.04,
        extension_seconds=600,
        run_ledger=ledger,  # type: ignore[arg-type]
        run_id="run-hb-fail",
        stage="parse",
    )

    extender.start()
    await asyncio.sleep(0.17)
    await extender.stop()

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
