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


def test_dlq_reconciler_jobs_have_ttl_cleanup() -> None:
    text = (ROOT / "deploy/k8s/workers/dlq-reconciler-cronjob.yaml").read_text()

    assert text.count("ttlSecondsAfterFinished: 1800") == 2


def test_cloud_build_smoke_tests_dlq_reconciler_cli_and_pins_cron_image() -> None:
    for relative_path, environment in [
        ("deploy/cloudbuild-staging.yaml", "staging"),
        ("deploy/cloudbuild.yaml", "production"),
    ]:
        text = (ROOT / relative_path).read_text()
        assert "id: 'smoke-pipeline-cli'" in text
        assert "pipeline_control reconcile-dlq --help" in text
        assert (
            "deploy/k8s/workers/dlq-reconciler-cronjob.yaml "
            f"-l environment={environment}"
        ) in text
        assert "kubectl set image cronjob/unity-pipeline-dlq-reconciler" in text


def test_cloud_build_applies_unity_external_secret_manifests() -> None:
    staging = (ROOT / "deploy/cloudbuild-staging.yaml").read_text()
    production = (ROOT / "deploy/cloudbuild.yaml").read_text()
    assert "unity-secrets-external-secret_staging.yaml" in staging
    assert "unity-secrets-external-secret_production.yaml" in production


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


def test_comms_deploy_carries_the_pipeline_backends_the_control_plane_probes() -> None:
    """The comms service must name the same bucket and project as its workers.

    ``/infra/pipeline/health`` answers ``ok`` only when both the artifact
    bucket and the Pub/Sub project resolve, and assistants read that answer to
    decide whether a fleet exists at all. Without ``UNIFY_PUBSUB_PROJECT_ID``
    the project stayed empty, health reported unusable, and every file
    ingestion parsed in the assistant's own process instead of dispatching --
    the one boundary the tier rule exists to hold. This caught that omission
    on staging after the control plane itself was already deployed and
    answering.

    The values are asserted against the worker manifests rather than written
    twice, because a plane that mints upload targets in one bucket while the
    workers read another is a worse failure than no plane at all: the dispatch
    is accepted and the work goes nowhere.
    """
    for relative_path, worker_manifest in [
        (
            "deploy/cloudbuild-staging.yaml",
            "deploy/k8s/workers/ingest-worker-deployment_staging.yaml",
        ),
        (
            "deploy/cloudbuild.yaml",
            "deploy/k8s/workers/ingest-worker-deployment.yaml",
        ),
    ]:
        worker = (ROOT / worker_manifest).read_text()
        bucket = _manifest_env_value(worker, "UNIFY_GCS_ARTIFACT_BUCKET")
        environment = _manifest_env_value(worker, "UNIFY_GCP_PIPELINE_ENVIRONMENT")
        project = _manifest_env_value(worker, "UNIFY_PUBSUB_PROJECT_ID")

        text = (ROOT / relative_path).read_text()
        assert f"UNIFY_GCS_ARTIFACT_BUCKET={bucket}" in text
        assert f"UNIFY_GCP_PIPELINE_ENVIRONMENT={environment}" in text
        assert "UNIFY_PUBSUB_PROJECT_ID=${PROJECT_ID}" in text
        # ``${PROJECT_ID}`` is the build's own project, which is where the
        # workers' topics live; pinning the literal here would drift instead.
        assert project == "gcp-project-runtime"


def _manifest_env_value(manifest_text: str, name: str) -> str:
    """Read one ``name``/``value`` env pair out of a k8s manifest."""
    lines = manifest_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {name}":
            return lines[index + 1].split("value:", 1)[1].strip().strip('"')
    raise AssertionError(f"{name} not found in manifest")


def test_cloud_build_worker_rollout_has_independent_availability_timeout() -> None:
    for relative_path in [
        "deploy/cloudbuild-staging.yaml",
        "deploy/cloudbuild.yaml",
    ]:
        text = (ROOT / relative_path).read_text()
        assert "rs_deadline=$$(($$SECONDS + 300))" in text
        assert "availability_deadline=$$(($$SECONDS + 900))" in text
        assert "progress: current=$$current ready=$$ready available=$$available" in text
        assert 'kubectl describe pods -n "$$namespace"' in text
