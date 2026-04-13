from pathlib import Path

import pytest

from tests.infra.integration import conftest as integration_conftest


def test_poll_until_includes_failure_snapshot():
    with pytest.raises(TimeoutError) as exc_info:
        integration_conftest.poll_until(
            lambda: False,
            timeout=0.01,
            interval=0,
            description="example condition",
            failure_snapshot=lambda: {"assistant_id": "42", "phase": "PendingVM"},
        )

    text = str(exc_info.value)
    assert "Failure snapshot" in text
    assert '"assistant_id": "42"' in text
    assert '"phase": "PendingVM"' in text


def test_cloud_run_service_name_parses_staging_url():
    service_name = integration_conftest._cloud_run_service_name(
        "https://unity-comms-app-staging-000000000000.us-central1.run.app",
    )
    assert service_name == "unity-comms-app-staging"


def test_write_failure_artifact_writes_expected_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifact_path = integration_conftest._write_failure_artifact(
        {
            "test_nodeid": "tests/infra/integration/test_example.py::test_case",
            "timestamp": "2026-03-31T00:00:00Z",
            "assistant_ids": ["42"],
        },
    )

    path = Path(artifact_path)
    assert path.exists()
    assert path.parent.name == "integration-failures"
    assert "test_case" in path.name
