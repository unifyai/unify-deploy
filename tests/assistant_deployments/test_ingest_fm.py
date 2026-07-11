"""Tests for FileManager ingestion script helpers."""

from __future__ import annotations

from types import SimpleNamespace

from unify_deploy.assistant_deployments.scripts import ingest_fm
from unify_deploy.assistant_deployments.types.pipeline_config import PipelineConfig


def test_dispatch_fm_passes_table_config_and_document_only_files(monkeypatch):
    cfg = PipelineConfig.model_validate(
        {
            "source_files": [
                {
                    "file_path": "helpful_context.pdf",
                    "tables": [],
                },
                {
                    "file_path": "orders.csv",
                    "tables": [
                        {
                            "sheet": "Orders",
                            "context": "Data/Orders",
                            "description": "Order rows",
                        },
                    ],
                },
            ],
        },
    )
    monkeypatch.setenv("UNITY_PUBSUB_PROJECT_ID", "test-project")
    monkeypatch.setenv("UNITY_GCS_ARTIFACT_BUCKET", "test-bucket")

    calls = []

    def fake_publish_parse_request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            job_id=f"job-{len(calls)}",
            gs_uri=f"gs://bucket/source-{len(calls)}",
            message_id=f"msg-{len(calls)}",
        )

    import unify.common.pipeline as pipeline

    monkeypatch.setattr(
        pipeline,
        "publish_parse_request",
        fake_publish_parse_request,
    )

    exit_code = ingest_fm._dispatch_fm(
        config=cfg,
        project_name="Project",
        user_id="user-1",
        assistant_id="assistant-1",
        alias="Local",
    )

    assert exit_code == 0
    assert len(calls) == 2
    assert calls[0]["logical_path"] == "helpful_context.pdf"
    assert calls[0]["table_config"] is None
    assert calls[0]["fm_binding"].user_id == "user-1"
    assert calls[0]["fm_binding"].assistant_id == "assistant-1"
    assert calls[1]["table_config"]["Orders"]["context"] == "Data/Orders"
    assert calls[1]["table_config"]["Orders"]["description"] == "Order rows"
