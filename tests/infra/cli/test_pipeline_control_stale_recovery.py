from __future__ import annotations

from types import SimpleNamespace

from unify.common.pipeline.types import (
    FileParseResult,
    InlineRowsHandle,
    IngestCheckpoint,
    IngestPlan,
    TableMeta,
)
from unity_deploy.infra.cli import pipeline_control
from unity_deploy.infra.gcp.pipeline_observability import JobObservabilitySnapshot


class _ArtifactStore:
    def __init__(self, manifest: IngestPlan, payload: dict | None = None) -> None:
        self.manifest = manifest
        self.payload = payload

    def get_json(self, key: str):
        if key == "jobs/job-1/outbox/parse.json":
            if self.payload is None:
                raise FileNotFoundError(key)
            return {"payload": self.payload}
        if key == "jobs/job-1/manifests/demo.json":
            return self.manifest.model_dump(mode="json")
        raise FileNotFoundError(key)


class _JobStore:
    def read_job(self, _job_id: str):
        return SimpleNamespace(status="running", metadata={}, dispatch_id="dispatch-1")


def _manifest() -> IngestPlan:
    return IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=[
            TableMeta(table_id="table_1", label="table_1", columns=["a"], row_count=10),
        ],
        table_inputs={
            "table_1": InlineRowsHandle(
                rows=[{"a": i} for i in range(10)],
                columns=["a"],
                row_count=10,
            ),
        },
    )


def _payload() -> dict:
    return {
        "kind": "ingest_requested",
        "job_id": "job-1",
        "dispatch_id": "dispatch-1",
        "manifest_key": "jobs/job-1/manifests/demo.json",
        "ingestion_mode": "dm",
    }


def _snapshot(checkpoint_rows: int) -> JobObservabilitySnapshot:
    checkpoints = {}
    if checkpoint_rows:
        checkpoints["table_1"] = IngestCheckpoint(
            job_id="job-1",
            artifact_id="table_1",
            rows_committed=checkpoint_rows,
            chunks_committed=max(1, checkpoint_rows // 5),
        )
    return JobObservabilitySnapshot(
        job_id="job-1",
        dispatch_id="dispatch-1",
        durable_status="running",
        derived_status="running-stale",
        retry_classification="operator_retryable",
        retry_eligible=True,
        checkpoints=checkpoints,
    )


def _patch_stores(monkeypatch, artifact_store):
    infra = SimpleNamespace()
    monkeypatch.setattr(
        pipeline_control,
        "_get_artifact_store",
        lambda _infra: artifact_store,
    )
    monkeypatch.setattr(pipeline_control, "_get_job_store", lambda _infra: _JobStore())
    return infra


def test_plan_stale_recovery_republishes_partial_parse_outbox(monkeypatch) -> None:
    artifact_store = _ArtifactStore(_manifest(), _payload())
    infra = _patch_stores(monkeypatch, artifact_store)
    monkeypatch.setattr(
        pipeline_control,
        "_load_job_snapshot",
        lambda *_args, **_kwargs: _snapshot(5),
    )

    plan, skipped = pipeline_control._plan_stale_recovery(
        infra,
        ["job-1"],
        max_jobs=0,
        max_attempts=3,
        force=False,
    )

    assert skipped == []
    assert plan[0]["action"] == "republish_ingest"
    assert plan[0]["payload_source"] == "parse_outbox"
    assert plan[0]["resume_rows"] == 5


def test_plan_stale_recovery_finalizes_complete_checkpoint(monkeypatch) -> None:
    artifact_store = _ArtifactStore(_manifest(), _payload())
    infra = _patch_stores(monkeypatch, artifact_store)
    monkeypatch.setattr(
        pipeline_control,
        "_load_job_snapshot",
        lambda *_args, **_kwargs: _snapshot(10),
    )

    plan, _skipped = pipeline_control._plan_stale_recovery(
        infra,
        ["job-1"],
        max_jobs=0,
        max_attempts=3,
        force=False,
    )

    assert plan[0]["action"] == "finalize_success"
    assert plan[0]["checkpoint_complete"] is True


def test_plan_stale_recovery_marks_missing_payload_needs_operator(monkeypatch) -> None:
    artifact_store = _ArtifactStore(_manifest(), None)
    infra = _patch_stores(monkeypatch, artifact_store)
    monkeypatch.setattr(
        pipeline_control,
        "_load_job_snapshot",
        lambda *_args, **_kwargs: _snapshot(5),
    )

    plan, _skipped = pipeline_control._plan_stale_recovery(
        infra,
        ["job-1"],
        max_jobs=0,
        max_attempts=3,
        force=False,
    )

    assert plan[0]["action"] == "needs_operator"
    assert plan[0]["payload_source"] == "missing"


def test_plan_stale_recovery_includes_queued_stale(monkeypatch) -> None:
    artifact_store = _ArtifactStore(_manifest(), _payload())
    infra = _patch_stores(monkeypatch, artifact_store)
    queued_stale_snapshot = JobObservabilitySnapshot(
        job_id="job-1",
        dispatch_id="dispatch-1",
        durable_status="queued",
        derived_status="queued-stale",
        retry_classification="operator_retryable",
        retry_eligible=True,
        checkpoints={},
    )
    monkeypatch.setattr(
        pipeline_control,
        "_load_job_snapshot",
        lambda *_args, **_kwargs: queued_stale_snapshot,
    )

    class _QueuedJobStore:
        def read_job(self, _job_id: str):
            return SimpleNamespace(
                status="queued",
                metadata={},
                dispatch_id="dispatch-1",
            )

    monkeypatch.setattr(
        pipeline_control,
        "_get_job_store",
        lambda _infra: _QueuedJobStore(),
    )

    plan, skipped = pipeline_control._plan_stale_recovery(
        infra,
        ["job-1"],
        max_jobs=0,
        max_attempts=3,
        force=False,
    )

    assert skipped == []
    assert plan[0]["action"] == "republish_ingest"
    assert plan[0]["derived_status"] == "queued-stale"


# ---------------------------------------------------------------------------
# Phase 0: cross-command in-flight publish guard
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone


def _job(*, status: str, metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(status=status, metadata=metadata, dispatch_id="dispatch-1")


def test_recent_inflight_publish_flags_fresh_retry_marker() -> None:
    job = _job(
        status="queued",
        metadata={
            "last_retry_message_id": "msg-retry-1",
            "last_publish_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    reason = pipeline_control._recent_inflight_publish(job)
    assert reason is not None
    assert "msg-retry-1" in reason
    assert "retry" in reason


def test_recent_inflight_publish_flags_fresh_stale_recovery_marker() -> None:
    """recover-stale's marker must also block a retry publish (cross-command)."""
    job = _job(
        status="queued",
        metadata={
            "last_stale_recovery_message_id": "msg-stale-1",
            "last_publish_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    reason = pipeline_control._recent_inflight_publish(job)
    assert reason is not None
    assert "msg-stale-1" in reason
    assert "recover-stale" in reason


def test_recent_inflight_publish_ignores_stale_marker() -> None:
    """A marker older than the guard window is treated as a lost message."""
    old = datetime.now(timezone.utc) - timedelta(
        seconds=pipeline_control._INFLIGHT_GUARD_SECONDS + 60,
    )
    job = _job(
        status="queued",
        metadata={
            "last_retry_message_id": "msg-old",
            "last_publish_at": old.isoformat(),
        },
    )
    assert pipeline_control._recent_inflight_publish(job) is None


def test_recent_inflight_publish_ignores_terminal_status() -> None:
    job = _job(
        status="success",
        metadata={
            "last_retry_message_id": "msg-1",
            "last_publish_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert pipeline_control._recent_inflight_publish(job) is None


def test_plan_stale_recovery_skips_when_retry_inflight(monkeypatch) -> None:
    """recover-stale must skip a job a recent retry already published to."""
    artifact_store = _ArtifactStore(_manifest(), _payload())

    class _InflightJobStore:
        def read_job(self, _job_id: str):
            return _job(
                status="queued",
                metadata={
                    "last_retry_message_id": "msg-retry-1",
                    "last_publish_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    infra = SimpleNamespace()
    monkeypatch.setattr(
        pipeline_control,
        "_get_artifact_store",
        lambda _infra: artifact_store,
    )
    monkeypatch.setattr(
        pipeline_control,
        "_get_job_store",
        lambda _infra: _InflightJobStore(),
    )
    monkeypatch.setattr(
        pipeline_control,
        "_load_job_snapshot",
        lambda *_args, **_kwargs: _snapshot(5),
    )

    plan, skipped = pipeline_control._plan_stale_recovery(
        infra,
        ["job-1"],
        max_jobs=0,
        max_attempts=3,
        force=False,
    )

    assert plan == []
    assert len(skipped) == 1
    assert "in-flight" in skipped[0]["reason"]
    assert "msg-retry-1" in skipped[0]["reason"]


# ---------------------------------------------------------------------------
# Phase 0: verify subcommand (declared row_count vs durable checkpoint)
# ---------------------------------------------------------------------------


def _patch_verify(monkeypatch, *, checkpoint_rows: int):
    artifact_store = _ArtifactStore(_manifest(), _payload())
    infra = _patch_stores(monkeypatch, artifact_store)

    checkpoints = {}
    if checkpoint_rows:
        checkpoints["table_1"] = IngestCheckpoint(
            job_id="job-1",
            artifact_id="table_1",
            rows_committed=checkpoint_rows,
            chunks_committed=max(1, checkpoint_rows // 5),
        )
    monkeypatch.setattr(
        pipeline_control,
        "_get_job_store",
        lambda _infra: _JobStore(),
    )
    from unity_deploy.infra.gcp import pipeline_observability

    monkeypatch.setattr(
        pipeline_observability,
        "list_job_checkpoints",
        lambda _store, _job_id: checkpoints,
    )
    return infra


def test_verify_jobs_passes_when_checkpoint_matches_declared(monkeypatch) -> None:
    infra = _patch_verify(monkeypatch, checkpoint_rows=10)

    results, all_ok = pipeline_control._verify_jobs(infra, ["job-1"])

    assert all_ok is True
    assert results[0]["ok"] is True
    assert results[0]["tables"][0]["rows_committed"] == 10
    assert results[0]["tables"][0]["expected_rows"] == 10


def test_verify_jobs_fails_when_checkpoint_short(monkeypatch) -> None:
    infra = _patch_verify(monkeypatch, checkpoint_rows=6)

    results, all_ok = pipeline_control._verify_jobs(infra, ["job-1"])

    assert all_ok is False
    assert results[0]["ok"] is False
    assert "short" in results[0]["reason"]
    assert "6/10" in results[0]["reason"]
