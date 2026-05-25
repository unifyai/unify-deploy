from __future__ import annotations

from types import SimpleNamespace

from unity.common.pipeline.types import (
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
