"""Unit tests for parse worker helpers."""

from __future__ import annotations

import pytest

from unity.common.pipeline.types import FileParseResult, IngestPlan, TableMeta
from unity_deploy.infra.workers.parse_worker import _merge_table_config


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
