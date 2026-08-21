"""Unit tests for parse worker helpers."""

from __future__ import annotations

import pytest

from unify.common.pipeline.types import FileParseResult, IngestPlan, TableMeta
from unify.common.pipeline.work_queue import ReceivedWorkItem
from unify_deploy.infra.gcp.artifact_store import LeaseNotAcquired, LeaseRecord
from unify_deploy.infra.workers import parse_worker
from unify_deploy.infra.workers.parse_worker import _merge_table_config
from unify_deploy.infra.workers.worker_utils import DuplicateLiveAttempt


def _plan_with_tables(*tables: TableMeta) -> IngestPlan:
    return IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=list(tables),
        table_inputs={},
    )


def test_merge_table_config_applies_single_csv_config_by_position() -> None:
    """Single-table CSV configs can use business names instead of parser labels."""
    plan = _plan_with_tables(
        TableMeta(
            table_id="table_1",
            label="table:1",
            columns=["id"],
            row_count=1,
        ),
    )

    merged = _merge_table_config(
        plan,
        {
            "worksOrderCategories": {
                "context": "Data/WorksOrderCategories",
                "description": "Business table description",
                "chunk_size": 500,
            },
        },
    )

    meta = merged.tables_meta[0]
    assert meta.context == "Data/WorksOrderCategories"
    assert meta.description == "Business table description"
    assert meta.chunk_size == 500


def test_merge_table_config_rejects_unmatched_multi_table_config() -> None:
    """Multi-table config must still match parsed table identities explicitly."""
    plan = _plan_with_tables(
        TableMeta(table_id="table_1", label="First", columns=["id"], row_count=1),
        TableMeta(table_id="table_2", label="Second", columns=["id"], row_count=1),
    )

    with pytest.raises(ValueError, match="doesNotExist"):
        _merge_table_config(
            plan,
            {
                "First": {"context": "Data/First"},
                "doesNotExist": {"context": "Data/Missing"},
            },
        )


@pytest.mark.asyncio
async def test_replay_parse_outbox_treats_missing_artifact_as_no_replay() -> None:
    from unify.common.pipeline.artifact_store import ArtifactNotFound

    class _Store:
        def get_json(self, key: str):
            raise ArtifactNotFound(f"Artifact not found: {key}")

    replayed = await parse_worker._replay_parse_outbox_if_needed(
        _Store(),
        work_queue=None,
        run_id="dc00ad723eaf4015ab06006e3294c63f",
    )
    assert replayed is False


@pytest.mark.asyncio
async def test_parse_duplicate_live_lease_raises_duplicate_attempt(monkeypatch) -> None:
    async def _no_outbox(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        parse_worker,
        "_replay_parse_outbox_if_needed",
        _no_outbox,
    )

    class _Ledger:
        def close(self):
            pass

    class _JobStore:
        def read_job(self, _job_id):
            return type("_Job", (), {"status": "queued"})()

    class _ArtifactStore:
        def acquire_lease(self, *_args, **_kwargs):
            raise LeaseNotAcquired(
                "live parse owner",
                lease=LeaseRecord(
                    key="jobs/job-1/leases/parse.json",
                    owner_id="parse:pod-a",
                    attempt_id="attempt-a",
                    stage="parse",
                    acquired_at="2026-05-06T00:00:00+00:00",
                    heartbeat_at="2026-05-06T00:00:00+00:00",
                    expires_at="2026-05-06T00:15:00+00:00",
                    generation=1,
                ),
            )

    class _Infra:
        artifact_store = _ArtifactStore()
        job_store = _JobStore()
        work_queue = object()

        @staticmethod
        def run_ledger_factory(_run_id):
            return _Ledger()

    item = ReceivedWorkItem(
        message_id="parse-msg-1",
        topic="parse",
        payload={
            "kind": "parse_requested",
            "job_id": "job-1",
            "dispatch_id": "dispatch-1",
            "file_paths": ["gs://bucket/source.csv"],
        },
        receipt_id="ack-1",
    )

    with pytest.raises(DuplicateLiveAttempt) as exc:
        await parse_worker.handle_parse_message(item, infra=_Infra())

    assert exc.value.stage == "parse"
    assert exc.value.lease.owner_id == "parse:pod-a"
