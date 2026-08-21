"""Table-spec lookup must be per source file, not per sheet name."""

from __future__ import annotations

from pathlib import Path

from unify.common.pipeline.config import PipelineConfig
from unify_deploy.assistant_deployments.scripts.ingest_dm import (
    _index_column_descriptions,
    _index_table_specs,
    _lookup_table_spec,
)


def test_table_specs_are_keyed_by_file_and_sheet(tmp_path: Path) -> None:
    week_a = tmp_path / "2026_07_13" / "dimDate.csv"
    week_b = tmp_path / "2026_08_10" / "dimDate.csv"
    week_a.parent.mkdir(parents=True)
    week_b.parent.mkdir(parents=True)
    week_a.write_text("Date\n2026-07-13\n")
    week_b.write_text("Date\n2026-08-10\n")

    cfg = PipelineConfig.model_validate(
        {
            "source_files": [
                {
                    "file_path": str(week_a),
                    "tables": [
                        {
                            "sheet": "dimDate",
                            "context": "ClientBeta/ClientAlpha/v3/2026_07_13/Repairs/Dimensions/Dates",
                        },
                    ],
                },
                {
                    "file_path": str(week_b),
                    "tables": [
                        {
                            "sheet": "dimDate",
                            "context": "ClientBeta/ClientAlpha/v3/2026_08_10/Repairs/Dimensions/Dates",
                        },
                    ],
                },
            ],
            "ingest": {
                "business_contexts": {
                    "file_contexts": [
                        {
                            "file_path": str(week_a),
                            "table_contexts": [
                                {
                                    "table": "dimDate",
                                    "column_descriptions": {"Date": "week A date"},
                                },
                            ],
                        },
                        {
                            "file_path": str(week_b),
                            "table_contexts": [
                                {
                                    "table": "dimDate",
                                    "column_descriptions": {"Date": "week B date"},
                                },
                            ],
                        },
                    ],
                },
            },
        },
    )

    specs = _index_table_specs(cfg)
    assert _lookup_table_spec(specs, str(week_a), "dimDate").context.endswith(
        "2026_07_13/Repairs/Dimensions/Dates",
    )
    assert _lookup_table_spec(specs, str(week_b), "dimDate").context.endswith(
        "2026_08_10/Repairs/Dimensions/Dates",
    )
    assert _lookup_table_spec(specs, str(week_a), "missing") is None

    col_descs = _index_column_descriptions(cfg)
    from unify_deploy.assistant_deployments.scripts.ingest_dm import (
        _resolved_source_path,
    )

    assert col_descs[(_resolved_source_path(str(week_a)), "dimDate")]["Date"] == (
        "week A date"
    )
    assert col_descs[(_resolved_source_path(str(week_b)), "dimDate")]["Date"] == (
        "week B date"
    )
