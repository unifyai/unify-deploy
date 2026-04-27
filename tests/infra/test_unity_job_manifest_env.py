from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_create_unity_job_uses_explicit_env_allowlist() -> None:
    text = (ROOT / "communication/infra/helpers.py").read_text()

    create_job_source = text[text.index("def create_unity_job(") :]
    create_job_source = create_job_source[
        : create_job_source.index("def get_job_logs(")
    ]
    assert '"envFrom"' not in create_job_source
    assert "GCP_SA_KEY" not in create_job_source
    assert '"ORCHESTRA_ADMIN_KEY"' in create_job_source
    assert '"GCP_PROJECT_ID"' in create_job_source
