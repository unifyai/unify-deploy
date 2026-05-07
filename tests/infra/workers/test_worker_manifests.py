from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKER_MANIFESTS = [
    ROOT / "deploy/k8s/workers/ingest-worker-deployment_staging.yaml",
    ROOT / "deploy/k8s/workers/parse-worker-deployment_staging.yaml",
    ROOT / "deploy/k8s/workers/ingest-worker-deployment.yaml",
    ROOT / "deploy/k8s/workers/parse-worker-deployment.yaml",
]


def test_pipeline_workers_use_workload_identity_service_account() -> None:
    for manifest in WORKER_MANIFESTS:
        text = manifest.read_text()
        assert "serviceAccountName: unity-pipeline-worker" in text
        assert "GCP_SA_KEY" not in text


def test_pipeline_worker_service_account_has_gcp_annotation() -> None:
    text = (ROOT / "deploy/k8s/workers/pipeline-worker-serviceaccount.yaml").read_text()

    assert text.count("name: unity-pipeline-worker") >= 2
    assert (
        "iam.gke.io/gcp-service-account: "
        "service-account@example.iam.gserviceaccount.com"
    ) in text


def test_cloud_build_applies_pipeline_worker_service_account() -> None:
    for relative_path in [
        "deploy/cloudbuild-staging.yaml",
        "deploy/cloudbuild.yaml",
    ]:
        text = (ROOT / relative_path).read_text()
        assert "deploy/k8s/workers/pipeline-worker-serviceaccount.yaml" in text


def test_job_watcher_uses_explicit_secret_allowlist() -> None:
    for relative_path in [
        "base/scripts/job-watcher/deployment_staging.yaml",
        "base/scripts/job-watcher/deployment.yaml",
    ]:
        text = (ROOT / relative_path).read_text()
        assert "envFrom:" not in text
        assert "GCP_SA_KEY" not in text
        assert "key: ORCHESTRA_ADMIN_KEY" in text


def test_deployment_reconcile_job_does_not_require_global_unify_key() -> None:
    text = (
        ROOT / "deploy/k8s/deployment-reconcile/deployment-reconcile-job.yaml"
    ).read_text()

    assert "key: ORCHESTRA_ADMIN_KEY" in text
    assert "name: UNIFY_KEY" not in text
    assert "key: UNIFY_KEY" not in text


def test_cloud_build_deployment_reconcile_is_control_plane_only() -> None:
    for relative_path in [
        "deploy/cloudbuild-staging.yaml",
        "deploy/cloudbuild.yaml",
    ]:
        text = (ROOT / relative_path).read_text()
        assert "--planes control-plane \\" in text
        assert "--planes control-plane,runtime" not in text
