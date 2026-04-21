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

import pytest

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
