"""Unit tests for ``unify_deploy.infra.workers.ingest_worker``.

These focus narrowly on the per-message ``UNIFY_KEY`` lifecycle managed
by :func:`_with_unify_key`:

* each message resolves its own key and installs it for the duration of
  the context only;
* the prior environment is restored after normal exit and after errors
  raised inside the message body;
* if key resolution fails before installation, the existing environment
  is left untouched.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from unify.common.pipeline import IngestRequested
from unify.common.pipeline.types import (
    DmBinding,
    FileParseResult,
    FmBinding,
    InlineRowsHandle,
    IngestPlan,
    ObjectStoreArtifactHandle,
    TableMeta,
)
from unify_deploy.infra.workers import ingest_worker
from unify_deploy.infra.workers import worker_utils
from unify_deploy.infra.workers.assistant_key_resolver import ResolvedAssistant

# Lease, checkpoint and completion invariants belong to the shared engine and are
# tested against the port in unify's tests/common/test_checkpointed_ingest.py, so
# both bindings are held to them. What is tested here is the worker's own
# surroundings: how it drives that engine and what it does with the outcome.


def _single_table_plan(*, row_count: int = 1) -> IngestPlan:
    return IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=[
            TableMeta(
                table_id="table_1",
                label="table_1",
                columns=["a"],
                row_count=row_count,
            ),
        ],
        table_inputs={
            "table_1": InlineRowsHandle(
                rows=[{"a": 1}] * row_count,
                columns=["a"],
                row_count=row_count,
            ),
        },
    )


def _stub_msg(**overrides):
    return SimpleNamespace(
        **{
            "job_id": "job-1",
            "dispatch_id": "dispatch-1",
            "batch_size": 100,
            "target_context": "ctx",
            "request_key": "",
            **overrides,
        },
    )


class _StubLedger:
    def __init__(self):
        self.entries = []

    def write(self, entry):
        self.entries.append(entry)

    def flush(self):
        pass

    def close(self):
        pass


class _StubInfra:
    class _Store:
        def read_checkpoint(self, *_args, **_kwargs):
            return None

    artifact_store = _Store()
    storage_client = None
    settings = SimpleNamespace(lease_ttl_seconds=900)


class _StubDataManager:
    """Stands in for DataManager so the worker can be driven without a backend."""

    def ingest(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_with_unify_key_swaps_env_per_message_and_cleans_up(
    monkeypatch,
) -> None:
    """Each message gets its own resolved key with no cross-message bleed."""
    monkeypatch.delenv("UNIFY_KEY", raising=False)

    bindings = [
        FmBinding(user_id="alice", assistant_id="42", logical_path="fm.csv"),
        DmBinding(user_id="alice", assistant_id="77", target_context="Orders"),
    ]
    resolved_keys = iter(["fm-key", "dm-key"])
    seen_before_install: list[tuple[str, str, str | None]] = []

    async def fake_resolve_assistant(binding):
        seen_before_install.append(
            (
                binding.user_id,
                binding.assistant_id or "",
                os.environ.get("UNIFY_KEY"),
            ),
        )
        return ResolvedAssistant(api_key=next(resolved_keys))

    monkeypatch.setattr(ingest_worker, "resolve_assistant", fake_resolve_assistant)

    async with ingest_worker._with_unify_key(bindings[0]) as resolved:
        assert resolved.api_key == "fm-key"
        assert os.environ["UNIFY_KEY"] == "fm-key"
    assert "UNIFY_KEY" not in os.environ

    async with ingest_worker._with_unify_key(bindings[1]) as resolved:
        assert resolved.api_key == "dm-key"
        assert os.environ["UNIFY_KEY"] == "dm-key"
    assert "UNIFY_KEY" not in os.environ

    assert seen_before_install == [
        ("alice", "42", None),
        ("alice", "77", None),
    ]


@pytest.mark.asyncio
async def test_with_unify_key_restores_previous_value_after_body_error(
    monkeypatch,
) -> None:
    """A message-specific key must not leak when the ingest body raises."""
    monkeypatch.setenv("UNIFY_KEY", "previous-key")

    async def fake_resolve_assistant(binding):
        return ResolvedAssistant(api_key="message-key")

    monkeypatch.setattr(ingest_worker, "resolve_assistant", fake_resolve_assistant)
    binding = DmBinding(
        user_id="alice",
        assistant_id="42",
        target_context="Orders",
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with ingest_worker._with_unify_key(binding) as resolved:
            assert resolved.api_key == "message-key"
            assert os.environ["UNIFY_KEY"] == "message-key"
            raise RuntimeError("boom")

    assert os.environ["UNIFY_KEY"] == "previous-key"


@pytest.mark.asyncio
async def test_with_unify_key_does_not_mutate_env_when_resolution_fails(
    monkeypatch,
) -> None:
    """Resolver failures happen before install, so the old env survives."""
    monkeypatch.setenv("UNIFY_KEY", "previous-key")

    async def fake_resolve_assistant(binding):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(ingest_worker, "resolve_assistant", fake_resolve_assistant)
    binding = FmBinding(user_id="alice", assistant_id="42", logical_path="x.csv")

    with pytest.raises(RuntimeError, match="lookup failed"):
        async with ingest_worker._with_unify_key(binding):
            pytest.fail("context body should never run when resolution fails")

    assert os.environ["UNIFY_KEY"] == "previous-key"


# ---------------------------------------------------------------------------
# Remote gs:// handle staging
# ---------------------------------------------------------------------------


class _FakeArtifactStore:
    """Minimal stub of GcsArtifactStore for staging tests."""

    def __init__(self) -> None:
        self.downloads: list[tuple[str, Path]] = []

    def download_to_local(self, source: str, dest: Path) -> Path:
        self.downloads.append((source, Path(dest)))
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text('{"id": 1}\n', encoding="utf-8")
        return dest_path


def test_scratch_dir_size_bytes_counts_nested_files(tmp_path) -> None:
    """Scratch pressure is the sum of files owned by the message scratch dir."""
    scratch_dir = tmp_path / "ingest_run_abc"
    nested = scratch_dir / "nested"
    nested.mkdir(parents=True)
    (scratch_dir / "source.csv").write_bytes(b"abcde")
    (nested / "sheet.jsonl").write_bytes(b"123")

    assert ingest_worker._scratch_dir_size_bytes(scratch_dir) == 8


def test_guard_scratch_usage_ignores_filesystem_wide_tmp_stats(
    tmp_path,
    monkeypatch,
) -> None:
    """The guard must not consult /tmp filesystem-wide overlay usage."""
    scratch_dir = tmp_path / "ingest_run_small"
    scratch_dir.mkdir()
    (scratch_dir / "tiny.csv").write_bytes(b"ok")

    def fail_if_old_overlay_guard_is_used():
        raise AssertionError("guard should not call tempfile.gettempdir()")

    monkeypatch.setattr(
        ingest_worker.tempfile,
        "gettempdir",
        fail_if_old_overlay_guard_is_used,
    )
    monkeypatch.setenv("UNITY_INGEST_TMP_MAX_BYTES", "10")

    ingest_worker._guard_scratch_usage(
        scratch_dir=scratch_dir,
        run_id="run-small",
        phase="after_staging",
    )


def test_guard_scratch_usage_raises_when_scratch_exceeds_threshold(
    tmp_path,
    monkeypatch,
) -> None:
    """Oversized staged source files still trip the worker scratch guard."""
    scratch_dir = tmp_path / "ingest_run_large"
    scratch_dir.mkdir()
    (scratch_dir / "large.csv").write_bytes(b"abcdef")
    monkeypatch.setenv("UNITY_INGEST_TMP_MAX_BYTES", "5")

    with pytest.raises(
        RuntimeError,
        match=(
            "ingest worker scratch usage exceeded guard threshold.*"
            "scratch_used=6.*threshold=5"
        ),
    ):
        ingest_worker._guard_scratch_usage(
            scratch_dir=scratch_dir,
            run_id="run-large",
            phase="after_staging",
        )


def test_guard_scratch_usage_treats_missing_scratch_dir_as_empty(
    tmp_path,
    monkeypatch,
) -> None:
    """Cleanup may remove the scratch dir before the post-ingest guard runs."""
    monkeypatch.setenv("UNITY_INGEST_TMP_MAX_BYTES", "1")

    ingest_worker._guard_scratch_usage(
        scratch_dir=tmp_path / "missing",
        run_id="run-clean",
        phase="after_durable_ingest",
    )


@pytest.mark.asyncio
async def test_handle_ingest_message_finalizes_success_before_ack(monkeypatch) -> None:
    events: list[str] = []
    plan = IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=[
            TableMeta(table_id="table_1", label="table_1", columns=["a"], row_count=1),
        ],
        table_inputs={
            "table_1": InlineRowsHandle(
                rows=[{"a": 1}],
                columns=["a"],
                row_count=1,
            ),
        },
    )

    class _ArtifactStore:
        def get_json(self, key):
            assert key == "jobs/job-1/manifests/demo.json"
            return plan.model_dump(mode="json")

        def read_checkpoint(self, _job_id, _table_id):
            # A completed ingest leaves a checkpoint matching the declared
            # row_count so the finalization gate accepts the success.
            return SimpleNamespace(rows_committed=1, chunks_committed=1)

    class _JobStore:
        def __init__(self):
            self.job = SimpleNamespace(
                job_id="job-1",
                dispatch_id="dispatch-1",
                status="running",
                finished_at=None,
                error=None,
                metadata={},
            )

        def read_job(self, job_id):
            assert job_id == "job-1"
            return self.job

        def upsert_job(self, job):
            events.append(f"upsert:{job.status}")
            self.job = job

    class _Queue:
        async def is_cancelled(self, _job_id):
            return False

    class _Ledger:
        def write(self, _entry):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _Watch:
        is_cancelled = None

        def paused(self):
            return False

        async def stop(self):
            pass

    infra = SimpleNamespace(
        artifact_store=_ArtifactStore(),
        work_queue=_Queue(),
        job_store=_JobStore(),
        run_ledger_factory=lambda _run_id: _Ledger(),
        settings=SimpleNamespace(
            environment="test",
            pubsub=SimpleNamespace(project_id="proj"),
        ),
    )
    monkeypatch.setattr(worker_utils, "_shutdown_event", None)
    monkeypatch.setattr(ingest_worker, "_spawn_control_watcher", lambda *_a: _Watch())
    monkeypatch.setattr(ingest_worker, "_mark_ingest_running", lambda **_kwargs: None)
    monkeypatch.setattr(
        ingest_worker,
        "_stage_remote_handles",
        lambda plan, **_kwargs: plan,
    )
    monkeypatch.setattr(ingest_worker, "_guard_scratch_usage", lambda **_kwargs: None)
    monkeypatch.setattr(
        ingest_worker,
        "_delete_staged_scratch_files",
        lambda *_a, **_k: None,
    )

    async def _fake_run_dm_mode(**_kwargs):
        return 1, None, ["ctx"]

    monkeypatch.setattr(ingest_worker, "_run_dm_mode", _fake_run_dm_mode)

    item = SimpleNamespace(
        payload=IngestRequested(
            job_id="job-1",
            dispatch_id="dispatch-1",
            manifest_key="jobs/job-1/manifests/demo.json",
            ingestion_mode="dm",
            dm_binding=DmBinding(
                user_id="user-1",
                assistant_id="assistant-1",
                target_context="ctx",
            ),
        ).model_dump(mode="json"),
        message_id="msg-1",
        pubsub_message_id="msg-1",
        delivery_attempt=1,
        receipt_id="receipt-1",
        source_subscription="sub",
    )

    async def _ack():
        events.append("ack")

    acked = await ingest_worker.handle_ingest_message(
        item,
        infra=infra,
        ack_receipt=_ack,
    )

    assert acked is True
    assert events == ["upsert:success", "ack"]
    assert infra.job_store.job.metadata.get("rows_expected") == 1


# ---------------------------------------------------------------------------
# Phase 0: finalization correctness gate
# ---------------------------------------------------------------------------


def _completion_plan(*, row_count: int) -> IngestPlan:
    return IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=[
            TableMeta(
                table_id="table_1",
                label="table_1",
                columns=["a"],
                row_count=row_count,
            ),
        ],
        table_inputs={
            "table_1": InlineRowsHandle(
                rows=[{"a": 1}],
                columns=["a"],
                row_count=row_count,
            ),
        },
    )


async def _run_completion_gate_message(
    monkeypatch,
    *,
    declared_row_count: int,
    committed_rows: int,
    initial_metadata: dict | None = None,
    max_retries_env: str | None = None,
):
    """Drive handle_ingest_message for a DM job whose checkpoint is short.

    Returns ``(events, job_store, captured_events)`` so callers can assert on
    the finalized job state and the emitted pipeline events.
    """
    plan = _completion_plan(row_count=declared_row_count)
    events: list[str] = []
    captured_events: list[str] = []

    class _ArtifactStore:
        def get_json(self, key):
            return plan.model_dump(mode="json")

        def read_checkpoint(self, _job_id, _table_id):
            return SimpleNamespace(
                rows_committed=committed_rows,
                chunks_committed=committed_rows,
            )

    class _JobStore:
        def __init__(self):
            self.job = SimpleNamespace(
                job_id="job-1",
                dispatch_id="dispatch-1",
                status="running",
                finished_at=None,
                error=None,
                metadata=dict(initial_metadata or {}),
            )

        def read_job(self, _job_id):
            return self.job

        def upsert_job(self, job):
            events.append(f"upsert:{job.status}")
            self.job = job

    class _Queue:
        async def is_cancelled(self, _job_id):
            return False

    class _Ledger:
        def write(self, _entry):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _Watch:
        is_cancelled = None

        def paused(self):
            return False

        async def stop(self):
            pass

    infra = SimpleNamespace(
        artifact_store=_ArtifactStore(),
        work_queue=_Queue(),
        job_store=_JobStore(),
        run_ledger_factory=lambda _run_id: _Ledger(),
        settings=SimpleNamespace(
            environment="test",
            pubsub=SimpleNamespace(project_id="proj"),
        ),
    )
    if max_retries_env is not None:
        monkeypatch.setenv("UNITY_INGESTION_INCOMPLETE_MAX_RETRIES", max_retries_env)
    monkeypatch.setattr(worker_utils, "_shutdown_event", None)
    monkeypatch.setattr(ingest_worker, "_spawn_control_watcher", lambda *_a: _Watch())
    monkeypatch.setattr(ingest_worker, "_mark_ingest_running", lambda **_kwargs: None)
    monkeypatch.setattr(
        ingest_worker,
        "_stage_remote_handles",
        lambda plan, **_kwargs: plan,
    )
    monkeypatch.setattr(ingest_worker, "_guard_scratch_usage", lambda **_kwargs: None)
    monkeypatch.setattr(
        ingest_worker,
        "_delete_staged_scratch_files",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        ingest_worker,
        "write_job_event",
        lambda _store, event: captured_events.append(event.event_type),
    )

    async def _fake_run_dm_mode(**_kwargs):
        # Ingest "succeeds" without error, but the checkpoint is short.
        return committed_rows, None, ["ctx"]

    monkeypatch.setattr(ingest_worker, "_run_dm_mode", _fake_run_dm_mode)

    item = SimpleNamespace(
        payload=IngestRequested(
            job_id="job-1",
            dispatch_id="dispatch-1",
            manifest_key="jobs/job-1/manifests/demo.json",
            ingestion_mode="dm",
            dm_binding=DmBinding(
                user_id="user-1",
                assistant_id="assistant-1",
                target_context="ctx",
            ),
        ).model_dump(mode="json"),
        message_id="msg-1",
        pubsub_message_id="msg-1",
        delivery_attempt=1,
        receipt_id="receipt-1",
        source_subscription="sub",
    )

    async def _ack():
        events.append("ack")

    return infra, item, events, captured_events, _ack


@pytest.mark.asyncio
async def test_completion_gate_nacks_when_checkpoint_short(monkeypatch) -> None:
    """A short checkpoint must block success and nack for a resume."""
    from unify.common.pipeline.work_queue import RetryWorkItem

    infra, item, events, captured, ack = await _run_completion_gate_message(
        monkeypatch,
        declared_row_count=10,
        committed_rows=6,
    )

    with pytest.raises(RetryWorkItem):
        await ingest_worker.handle_ingest_message(item, infra=infra, ack_receipt=ack)

    # Never finalized success, never acked.
    assert "upsert:success" not in events
    assert "ack" not in events
    assert "job_incomplete" in captured
    job = infra.job_store.job
    assert job.status == "running"  # kept non-terminal so resume can proceed
    assert job.metadata.get("incomplete_retry_attempt") == 1
    assert job.metadata.get("rows_expected") == 10


@pytest.mark.asyncio
async def test_completion_gate_dead_letters_when_budget_exhausted(monkeypatch) -> None:
    """Once the resume budget is spent, the job errors out instead of looping."""
    infra, item, events, captured, ack = await _run_completion_gate_message(
        monkeypatch,
        declared_row_count=10,
        committed_rows=6,
        initial_metadata={"incomplete_retry_attempt": 1},
        max_retries_env="1",
    )

    with pytest.raises(RuntimeError, match="ingest incomplete"):
        await ingest_worker.handle_ingest_message(item, infra=infra, ack_receipt=ack)

    assert "ack" not in events
    job = infra.job_store.job
    assert job.status == "error"
    assert "incomplete" in (job.error or "")


@pytest.mark.asyncio
async def test_dm_mode_surfaces_an_engine_failure(monkeypatch) -> None:
    """An exception from the shared engine must reach the ledger, not be masked.

    The worker wraps the engine in bookkeeping; a bug there previously turned a
    real ingest failure into a silent zero-row success.
    """
    import unify.data_manager as data_manager_module
    from unify.common.pipeline import checkpointed_ingest

    monkeypatch.setattr(data_manager_module, "DataManager", _StubDataManager)

    def _boom(self, *_args, **_kwargs):
        raise RuntimeError("boom inside the engine")

    monkeypatch.setattr(checkpointed_ingest.CheckpointedIngest, "run", _boom)

    ledger = _StubLedger()
    rows, error, _contexts = await ingest_worker._run_dm_mode_inner(
        plan=_single_table_plan(),
        msg=_stub_msg(),
        infra=_StubInfra(),
        run_ledger=ledger,
        dm_binding=DmBinding(
            user_id="user-1",
            assistant_id="assistant-1",
            target_context="ctx",
        ),
        default_target="ctx",
        activate_unify_context=lambda **_kwargs: None,
    )

    assert rows == 0
    assert error == "boom inside the engine"
    assert ledger.entries[-1].status == "error"


@pytest.mark.asyncio
async def test_dm_mode_lets_a_surrender_propagate(monkeypatch) -> None:
    """A surrender must reach the entrypoint, not be finalized as an error.

    Finalizing it would ack the message and the run would never resume, losing
    everything after the last checkpoint. The engine raises ``RetryWorkItem``;
    the worker's job is to not swallow it.
    """
    import unify.data_manager as data_manager_module
    from unify.common.pipeline import checkpointed_ingest
    from unify.common.pipeline.work_queue import RetryWorkItem

    monkeypatch.setattr(data_manager_module, "DataManager", _StubDataManager)

    def _surrender(self, *_args, **_kwargs):
        raise RetryWorkItem(
            f"Job job-1 {checkpointed_ingest.SURRENDER_SENTINEL}; "
            "will resume from checkpoint",
        )

    monkeypatch.setattr(checkpointed_ingest.CheckpointedIngest, "run", _surrender)

    with pytest.raises(RetryWorkItem):
        await ingest_worker._run_dm_mode_inner(
            plan=_single_table_plan(),
            msg=_stub_msg(),
            infra=_StubInfra(),
            run_ledger=_StubLedger(),
            dm_binding=DmBinding(
                user_id="user-1",
                assistant_id="assistant-1",
                target_context="ctx",
            ),
            default_target="ctx",
            activate_unify_context=lambda **_kwargs: None,
        )


# ---------------------------------------------------------------------------
# Phase 1: attempt-lease release on shutdown/surrender
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    content_rows_handle=None,
    table_inputs=None,
) -> IngestPlan:
    return IngestPlan(
        run_id="run-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        content_rows_handle=content_rows_handle,
        tables_meta=(
            [TableMeta(table_id=k, label=k) for k in (table_inputs or {})]
            if table_inputs
            else []
        ),
        table_inputs=table_inputs or {},
    )


def test_stage_remote_handles_skips_jsonl_gs_artifacts(
    tmp_path,
) -> None:
    """JSONL gs:// handles are NOT staged — they stream directly from GCS."""
    content = ObjectStoreArtifactHandle(
        storage_uri="gs://bucket/art/__content__.jsonl",
        logical_path="demo.csv/content",
        artifact_format="jsonl",
    )
    table = ObjectStoreArtifactHandle(
        storage_uri="gs://bucket/art/table_1.jsonl",
        logical_path="demo.csv/t1",
        artifact_format="jsonl",
    )
    plan = _make_plan(
        content_rows_handle=content,
        table_inputs={"table_1": table},
    )

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert staged is plan, "JSONL handles stream from GCS; plan must not be rebuilt"
    assert store.downloads == [], "no downloads should occur for JSONL handles"


def test_stage_remote_handles_downloads_non_jsonl_gs_artifacts(
    tmp_path,
) -> None:
    """Non-JSONL gs:// handles still get downloaded and staged locally."""
    parquet_handle = ObjectStoreArtifactHandle(
        storage_uri="gs://bucket/art/table_1.parquet",
        logical_path="demo.csv/t1",
        artifact_format="parquet",
    )
    plan = _make_plan(table_inputs={"table_1": parquet_handle})

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert (
        staged is not plan
    ), "plan should be rebuilt when non-JSONL handles are staged"
    assert staged.table_inputs["table_1"].source_local_path
    assert Path(staged.table_inputs["table_1"].source_local_path).exists()

    uris = [src for src, _ in store.downloads]
    assert uris == ["gs://bucket/art/table_1.parquet"]


def test_stage_remote_handles_is_noop_for_inline_and_already_staged(
    tmp_path,
) -> None:
    """Inline handles and pre-staged handles are returned unchanged."""
    inline = InlineRowsHandle(rows=[{"a": 1}], columns=["a"], row_count=1)
    pre_staged = ObjectStoreArtifactHandle(
        storage_uri="gs://bucket/art/done.jsonl",
        logical_path="demo.csv/done",
        source_local_path="/tmp/already-here.jsonl",
        artifact_format="jsonl",
    )
    plan = _make_plan(
        content_rows_handle=inline,
        table_inputs={"done": pre_staged},
    )

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert staged is plan, "plan must not be rebuilt when there is nothing to stage"
    assert store.downloads == []


def test_stage_remote_handles_raises_when_store_has_no_downloader(
    tmp_path,
) -> None:
    """A misconfigured store surfaces loudly for non-JSONL formats."""
    handle = ObjectStoreArtifactHandle(
        storage_uri="gs://bucket/art/x.parquet",
        logical_path="demo.csv/x",
        artifact_format="parquet",
    )
    plan = _make_plan(table_inputs={"x": handle})

    class _BadStore:
        pass

    with pytest.raises(RuntimeError, match="download_to_local"):
        ingest_worker._stage_remote_handles(
            plan,
            artifact_store=_BadStore(),
            scratch_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Per-table context resolution (multi-sheet XLSX support)
# ---------------------------------------------------------------------------


def test_table_meta_context_used_over_default_target():
    """Each table's meta.context overrides the message-level default_target.

    This is the core mechanism for multi-sheet XLSX files where each
    sheet targets a different DM context.
    """
    handle_a = InlineRowsHandle(rows=[{"x": 1}], columns=["x"], row_count=1)
    handle_b = InlineRowsHandle(rows=[{"y": 2}], columns=["y"], row_count=1)

    plan = IngestPlan(
        run_id="run-ctx",
        file_path="multi_sheet.xlsx",
        parse_summary=FileParseResult(
            logical_path="multi_sheet.xlsx",
            status="success",
        ),
        tables_meta=[
            TableMeta(
                table_id="sheet_a",
                label="SheetA",
                columns=["x"],
                context="Org/v2/DRS/AprilJune24",
            ),
            TableMeta(
                table_id="sheet_b",
                label="SheetB",
                columns=["y"],
                context="Org/v2/DRS/SepDec24",
            ),
        ],
        table_inputs={"sheet_a": handle_a, "sheet_b": handle_b},
    )

    default_target = "Org/v2/DRS/FallbackContext"

    for meta in plan.tables_meta:
        resolved = meta.context or default_target
        assert resolved == meta.context, (
            f"Expected per-table context {meta.context!r}, "
            f"got fallback {default_target!r}"
        )


def test_table_meta_falls_back_to_default_when_context_absent():
    """Tables without an explicit context fall back to default_target."""
    plan = IngestPlan(
        run_id="run-fb",
        file_path="single.csv",
        parse_summary=FileParseResult(
            logical_path="single.csv",
            status="success",
        ),
        tables_meta=[
            TableMeta(table_id="t1", label="Orders", columns=["a"]),
        ],
        table_inputs={
            "t1": InlineRowsHandle(rows=[{"a": 1}], columns=["a"], row_count=1),
        },
    )

    default_target = "Adhoc/Orders"
    meta = plan.tables_meta[0]
    resolved = meta.context or default_target
    assert resolved == default_target


# ---------------------------------------------------------------------------
# _merge_table_config: context flows through parse worker merge
# ---------------------------------------------------------------------------


def test_merge_table_config_threads_context():
    """_merge_table_config picks up 'context' from table_config entries."""
    from unify_deploy.infra.workers.parse_worker import _merge_table_config

    plan = IngestPlan(
        run_id="run-merge",
        file_path="multi.xlsx",
        parse_summary=FileParseResult(
            logical_path="multi.xlsx",
            status="success",
        ),
        tables_meta=[
            TableMeta(
                table_id="s1",
                label="SheetA",
                sheet_name="SheetA",
                columns=["a"],
            ),
            TableMeta(
                table_id="s2",
                label="SheetB",
                sheet_name="SheetB",
                columns=["b"],
            ),
        ],
        table_inputs={},
    )

    table_config = {
        "SheetA": {
            "context": "Org/DRS/AprilJune24",
            "description": "Sheet A desc",
        },
        "SheetB": {
            "context": "Org/DRS/SepDec24",
            "description": "Sheet B desc",
        },
    }

    merged = _merge_table_config(plan, table_config)
    assert merged.tables_meta[0].context == "Org/DRS/AprilJune24"
    assert merged.tables_meta[1].context == "Org/DRS/SepDec24"
    assert merged.tables_meta[0].description == "Sheet A desc"
    assert merged.tables_meta[1].description == "Sheet B desc"


# ---------------------------------------------------------------------------
# CSV/XLSX gs:// staging (skip-tabular-materialization path)
# ---------------------------------------------------------------------------


from unify.common.pipeline.types import CsvFileHandle, XlsxSheetHandle


def test_stage_csv_handle_with_gs_uri(tmp_path) -> None:
    """A CsvFileHandle with gs:// storage_uri is downloaded to scratch."""
    csv_handle = CsvFileHandle(
        storage_uri="gs://bucket/data.csv",
        logical_path="data.csv",
        source_local_path="/nonexistent/data.csv",
        columns=["a", "b"],
    )
    plan = _make_plan(table_inputs={"t1": csv_handle})

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert staged is not plan
    staged_handle = staged.table_inputs["t1"]
    assert isinstance(staged_handle, CsvFileHandle)
    assert Path(staged_handle.source_local_path).exists()
    assert len(store.downloads) == 1
    assert store.downloads[0][0] == "gs://bucket/data.csv"


def test_stage_xlsx_handles_dedup_same_source(tmp_path) -> None:
    """Multiple XLSX sheets from the same gs:// source are downloaded once."""
    sheet1 = XlsxSheetHandle(
        storage_uri="gs://bucket/workbook.xlsx",
        logical_path="workbook.xlsx",
        source_local_path="/nonexistent/workbook.xlsx",
        sheet_name="Sheet1",
        columns=["a"],
    )
    sheet2 = XlsxSheetHandle(
        storage_uri="gs://bucket/workbook.xlsx",
        logical_path="workbook.xlsx",
        source_local_path="/nonexistent/workbook.xlsx",
        sheet_name="Sheet2",
        columns=["b"],
    )
    plan = _make_plan(table_inputs={"s1": sheet1, "s2": sheet2})

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert staged is not plan
    assert len(store.downloads) == 1, "same gs:// should be downloaded only once"
    local_s1 = staged.table_inputs["s1"].source_local_path
    local_s2 = staged.table_inputs["s2"].source_local_path
    assert local_s1 == local_s2, "both sheets should reference the same local file"
    assert Path(local_s1).exists()


def test_stage_csv_with_local_uri_is_noop(tmp_path) -> None:
    """A CsvFileHandle with a file:// URI does not need staging."""
    csv_handle = CsvFileHandle(
        storage_uri="file:///tmp/data.csv",
        logical_path="data.csv",
        source_local_path="/tmp/data.csv",
        columns=["a"],
    )
    plan = _make_plan(table_inputs={"t1": csv_handle})

    store = _FakeArtifactStore()
    staged = ingest_worker._stage_remote_handles(
        plan,
        artifact_store=store,
        scratch_dir=tmp_path,
    )

    assert staged is plan, "local URI handles should not trigger staging"
    assert store.downloads == []


# ---------------------------------------------------------------------------
# Control watcher (cancel + pause)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_watcher_trips_cancel_event() -> None:
    """status=cancelled should set cancel_event on the next poll tick."""

    class _Store:
        def __init__(self):
            self.status = "running"

        def get_json(self, key):
            return {"status": self.status}

    store = _Store()
    watch = ingest_worker._spawn_control_watcher(store, "run-1", interval=0.01)
    try:
        # First tick will see "running" and keep polling.
        store.status = "cancelled"
        # Give the watcher a few ticks to observe the flip.
        for _ in range(50):
            if watch.cancel_event.is_set():
                break
            await _asyncio_sleep(0.01)
        assert watch.cancelled() is True
        assert watch.paused() is False
        assert watch.is_cancelled() is True
    finally:
        await watch.stop()


@pytest.mark.asyncio
async def test_control_watcher_trips_pause_event() -> None:
    """status=paused should set pause_event on the next poll tick."""

    class _Store:
        def __init__(self):
            self.status = "running"

        def get_json(self, key):
            return {"status": self.status}

    store = _Store()
    watch = ingest_worker._spawn_control_watcher(store, "run-1", interval=0.01)
    try:
        store.status = "paused"
        for _ in range(50):
            if watch.pause_event.is_set():
                break
            await _asyncio_sleep(0.01)
        assert watch.paused() is True
        assert watch.cancelled() is False
        assert watch.is_cancelled() is True, (
            "pause should be surfaced through the shared is_cancelled() closure "
            "so hot loops raise PipelineCancelled just like on cancel"
        )
    finally:
        await watch.stop()


@pytest.mark.asyncio
async def test_control_watcher_stop_is_idempotent() -> None:
    """Calling stop() twice should not raise."""

    class _Store:
        def get_json(self, key):
            return {"status": "running"}

    watch = ingest_worker._spawn_control_watcher(_Store(), "run-1", interval=0.01)
    await watch.stop()
    await watch.stop()  # should be a no-op


@pytest.mark.asyncio
async def test_control_watcher_swallows_exceptions() -> None:
    """Transient GCS errors must not leak out of the background task."""

    class _Store:
        def get_json(self, key):
            raise RuntimeError("network error")

    watch = ingest_worker._spawn_control_watcher(_Store(), "run-1", interval=0.01)
    try:
        # Run for a few ticks; the watcher should stay alive and neither
        # event should trip.
        await _asyncio_sleep(0.05)
        assert watch.cancel_event.is_set() is False
        assert watch.pause_event.is_set() is False
    finally:
        await watch.stop()


async def _asyncio_sleep(seconds: float) -> None:
    import asyncio as _a

    await _a.sleep(seconds)


# ---------------------------------------------------------------------------
# _make_checkpoint_callback with cancellation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The staged request is the caller's intent, and a dispatched run honours it
# ---------------------------------------------------------------------------


def _staged_request(**target_overrides):
    from unify.ingestion_manager.types.request import (
        EmbedSpec,
        FilesSource,
        IngestionRequest,
        TableTarget,
    )

    return IngestionRequest(
        source=FilesSource(paths=["demo.csv"]),
        target=TableTarget(
            context="Data/Deals",
            unique_keys={"deal_id": "str"},
            fields={"deal_id": "str", "amount": "float"},
            infer_untyped_fields=True,
            **target_overrides,
        ),
        embed=EmbedSpec(columns=["notes"], strategy="after"),
    )


class TestStagedRequestOverlay:
    def test_the_request_options_reach_the_table_work(self):
        """Dropping these is the append-instead-of-upsert failure.

        Files always dispatch when a fleet is reachable, so if the caller's
        declared row identity does not survive the wire, re-submitting the same
        spreadsheet to the same table quietly appends a full second copy while
        the inline tier -- same request -- upserts. The two tiers must not
        disagree about what a request means.
        """
        work = ingest_worker._table_work_from_plan(
            _single_table_plan(),
            msg=_stub_msg(),
            default_target="ctx",
            request=_staged_request(),
        )
        entry = work[0]
        assert entry.unique_keys == {"deal_id": "str"}
        assert entry.fields == {"deal_id": "str", "amount": "float"}
        assert entry.embed_columns == ["notes"]
        assert entry.embed_strategy == "after"
        assert entry.infer_untyped_fields is True

    def test_without_a_request_the_parse_derived_config_stands(self):
        """Operator-CLI submits have no staged request and keep their behaviour."""
        work = ingest_worker._table_work_from_plan(
            _single_table_plan(),
            msg=_stub_msg(),
            default_target="ctx",
            request=None,
        )
        entry = work[0]
        assert entry.unique_keys is None
        assert entry.fields is None
        assert entry.infer_untyped_fields is False

    def test_an_unreadable_staged_request_fails_rather_than_defaulting(self):
        """Proceeding without the request would silently drop row identity —
        the exact failure the request exists to prevent — so the message must
        nack and retry once the store answers again."""
        from unify.common.pipeline.artifact_store import ArtifactNotFound

        class _Store:
            def get_json(self, key):
                raise ArtifactNotFound(key)

        with pytest.raises(ArtifactNotFound):
            ingest_worker._load_staged_request(
                _Store(),
                _stub_msg(request_key="jobs/run1/request.json"),
            )

    def test_a_missing_request_key_means_no_request(self):
        assert (
            ingest_worker._load_staged_request(object(), _stub_msg(request_key=""))
            is None
        )

    def test_a_collection_request_shapes_the_fm_config(self):
        """The caller's collection intent must survive dispatch: the shared
        name is what lets related files land in one namespace, and
        extract_tables=False is what keeps a report's layout tables from
        becoming noise contexts."""
        from unify.ingestion_manager.types.request import (
            CollectionTarget,
            FilesSource,
            IngestionRequest,
        )

        request = IngestionRequest(
            source=FilesSource(paths=["demo.pdf"]),
            target=CollectionTarget(name="Quarterly Reports", extract_tables=False),
        )
        config = ingest_worker._build_fm_config_from_plan(
            _single_table_plan(),
            request=request,
        )
        assert config.ingest.storage_id == "Quarterly Reports"
        assert config.ingest.table_ingest is False


class TestResultContexts:
    def test_content_and_table_contexts_are_collected_once_each(self):
        result = SimpleNamespace(
            content_ref=SimpleNamespace(context="u/1/Files/Local/7/Content"),
            tables_ref=[
                SimpleNamespace(context="u/1/Files/Local/7/Tables/Sheet1"),
                SimpleNamespace(context="u/1/Files/Local/7/Tables/Sheet1"),
            ],
        )
        assert ingest_worker._result_contexts(result) == [
            "u/1/Files/Local/7/Content",
            "u/1/Files/Local/7/Tables/Sheet1",
        ]

    def test_a_result_without_refs_reports_nothing(self):
        assert ingest_worker._result_contexts(SimpleNamespace()) == []
