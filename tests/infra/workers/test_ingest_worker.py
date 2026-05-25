"""Unit tests for ``unity_deploy.infra.workers.ingest_worker``.

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

from unity.common.pipeline import IngestRequested
from unity.common.pipeline.types import (
    DmBinding,
    FileParseResult,
    FmBinding,
    InlineRowsHandle,
    IngestPlan,
    ObjectStoreArtifactHandle,
    TableMeta,
)
from unity_deploy.infra.workers import ingest_worker
from unity_deploy.infra.gcp.artifact_store import LeaseNotAcquired, LeaseRecord
from unity_deploy.infra.workers import worker_utils
from unity_deploy.infra.workers.worker_utils import DuplicateLiveAttempt


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

    async def fake_resolve_api_key(binding):
        seen_before_install.append(
            (
                binding.user_id,
                binding.assistant_id or "",
                os.environ.get("UNIFY_KEY"),
            ),
        )
        return next(resolved_keys)

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)

    async with ingest_worker._with_unify_key(bindings[0]) as key:
        assert key == "fm-key"
        assert os.environ["UNIFY_KEY"] == "fm-key"
    assert "UNIFY_KEY" not in os.environ

    async with ingest_worker._with_unify_key(bindings[1]) as key:
        assert key == "dm-key"
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

    async def fake_resolve_api_key(binding):
        return "message-key"

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)
    binding = DmBinding(
        user_id="alice",
        assistant_id="42",
        target_context="Orders",
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with ingest_worker._with_unify_key(binding) as key:
            assert key == "message-key"
            assert os.environ["UNIFY_KEY"] == "message-key"
            raise RuntimeError("boom")

    assert os.environ["UNIFY_KEY"] == "previous-key"


@pytest.mark.asyncio
async def test_with_unify_key_does_not_mutate_env_when_resolution_fails(
    monkeypatch,
) -> None:
    """Resolver failures happen before install, so the old env survives."""
    monkeypatch.setenv("UNIFY_KEY", "previous-key")

    async def fake_resolve_api_key(binding):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)
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


def test_duplicate_live_ingest_lease_raises_before_dm_write(monkeypatch) -> None:
    """A duplicate delivery must not start DataManager work while a lease is fresh."""

    class Store:
        def acquire_lease(self, *args, **kwargs):
            raise LeaseNotAcquired(
                "live owner",
                lease=LeaseRecord(
                    key="jobs/job-1/leases/ingest-table_1.json",
                    owner_id="pod-a",
                    attempt_id="attempt-a",
                    stage="ingest",
                    acquired_at="2026-05-06T00:00:00+00:00",
                    heartbeat_at="2026-05-06T00:00:00+00:00",
                    expires_at="2026-05-06T00:15:00+00:00",
                    generation=3,
                ),
            )

    with pytest.raises(DuplicateLiveAttempt) as exc:
        ingest_worker._acquire_ingest_lease(
            Store(),
            job_id="job-1",
            table_id="table_1",
            attempt_id="attempt-b",
        )

    assert exc.value.stage == "ingest"
    assert exc.value.lease.owner_id == "pod-a"


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
        cost_ledger_factory=lambda _run_id: _Ledger(),
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
        return 1, None

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


@pytest.mark.asyncio
async def test_dm_mode_reports_early_ingest_artifacts_exception(monkeypatch) -> None:
    """Early DM ingest failures should not be masked by local bookkeeping bugs."""

    class _DataManager:
        def ingest(self, *args, **kwargs):
            return None

    import unity.data_manager as data_manager_module

    monkeypatch.setattr(data_manager_module, "DataManager", _DataManager)

    def fail_before_results(**_kwargs):
        raise RuntimeError("boom before artifact results")

    monkeypatch.setattr(ingest_worker, "ingest_artifacts", fail_before_results)

    class _ArtifactStore:
        def read_checkpoint(self, *_args, **_kwargs):
            return None

    class _Infra:
        artifact_store = _ArtifactStore()
        storage_client = None

    class _RunLedger:
        def __init__(self):
            self.entries = []

        def write(self, entry):
            self.entries.append(entry)

    plan = IngestPlan(
        run_id="job-1",
        file_path="demo.csv",
        parse_summary=FileParseResult(logical_path="demo.csv", status="success"),
        tables_meta=[
            TableMeta(
                table_id="table_1",
                label="table:1",
                columns=["a"],
                row_count=1,
            ),
        ],
        table_inputs={
            "table_1": InlineRowsHandle(
                rows=[{"a": 1}],
                columns=["a"],
                row_count=1,
            ),
        },
    )
    ledger = _RunLedger()

    rows, error = await ingest_worker._run_dm_mode_inner(
        plan=plan,
        msg=type(
            "_Msg",
            (),
            {
                "job_id": "job-1",
                "dispatch_id": "dispatch-1",
                "batch_size": 100,
            },
        )(),
        infra=_Infra(),
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
    assert error == "boom before artifact results"
    assert ledger.entries[-1].status == "error"


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
    from unity_deploy.infra.workers.parse_worker import _merge_table_config

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


from unity.common.pipeline.types import CsvFileHandle, XlsxSheetHandle


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

from unity.common.pipeline import PipelineCancelled


def test_checkpoint_callback_raises_on_cancellation(tmp_path) -> None:
    """The checkpoint callback should raise PipelineCancelled when cancelled."""

    class _Store:
        def write_checkpoint(self, *a, **kw):
            pass

    class _Task:
        task_type = "insert_chunk_rows"

    class _Result:
        value = {"row_count": 10}

    callback = ingest_worker._make_checkpoint_callback(
        _Store(),
        "job-1",
        "content",
        is_cancelled=lambda: True,
    )

    with pytest.raises(PipelineCancelled):
        callback(_Task(), _Result())


def test_checkpoint_callback_noop_when_not_cancelled(tmp_path) -> None:
    """The callback runs normally when is_cancelled returns False."""
    written = {"count": 0}

    class _Store:
        def write_checkpoint(self, *a, **kw):
            written["count"] += 1

    class _Task:
        task_type = "insert_chunk_rows"

    class _Result:
        value = {"row_count": 10}

    callback = ingest_worker._make_checkpoint_callback(
        _Store(),
        "job-1",
        "content",
        is_cancelled=lambda: False,
    )

    callback(_Task(), _Result())
    assert written["count"] == 1
