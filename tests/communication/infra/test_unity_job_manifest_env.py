"""Tests for the Unity assistant Job manifest contract.

After the ``build_unity_job_manifest`` / ``create_unity_job`` split,
manifest-shape assertions go directly against the dict returned by
the builder (no FakeBatchApi dance). Tests that verify the submit
side of the wrapper (calls ``create_namespaced_job`` exactly once,
returns the API response, swallows 409 Conflicts) live in their own
section at the bottom and use a tiny shared fake.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from communication.infra.helpers import (
    build_unity_job_manifest,
    create_unity_job,
)

ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _container(manifest: dict) -> dict:
    """The single ``unity-assistant`` container in the pod template."""
    return manifest["spec"]["template"]["spec"]["containers"][0]


def _env_by_name(manifest: dict) -> dict[str, dict]:
    """Map ``name -> env entry`` for the container's env list."""
    return {entry["name"]: entry for entry in _container(manifest)["env"]}


def _env_names(manifest: dict) -> list[str]:
    return [entry["name"] for entry in _container(manifest)["env"]]


# ---------------------------------------------------------------------------
# Manifest shape: env-var allowlist + sourcing rules
# ---------------------------------------------------------------------------


def test_no_envFrom_bulk_secret_or_configmap_injection() -> None:
    """Every env var is sourced explicitly (literal value, configMapKeyRef,
    or secretKeyRef). No bulk ``envFrom`` -- that was the shape the
    deleted legacy ``create_job.py`` used and is exactly what we don't
    want here, because it makes the per-env contract invisible.
    """
    manifest = build_unity_job_manifest(job_name="env-allowlist-staging")
    pod_spec = manifest["spec"]["template"]["spec"]
    assert "envFrom" not in pod_spec
    for container in pod_spec["containers"]:
        assert "envFrom" not in container


def test_openrouter_api_key_sourced_from_unity_secrets() -> None:
    manifest = build_unity_job_manifest(job_name="openrouter-key-staging")
    entry = _env_by_name(manifest)["OPENROUTER_API_KEY"]
    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "unity-secrets",
        "key": "OPENROUTER_API_KEY",
    }


def test_orchestra_admin_key_not_mounted_on_assistant_pods() -> None:
    """The platform admin key must never reach an assistant Job pod.

    Pods authenticate to Orchestra and the hosted gateway with their own
    per-assistant UNIFY_KEY against ownership-scoped routes; the fleet-wide
    ORCHESTRA_ADMIN_KEY stays on controllers / Cloud Run / reconcile jobs only.
    """
    manifest = build_unity_job_manifest(job_name="orch-admin-key-staging")
    assert "ORCHESTRA_ADMIN_KEY" not in _env_names(manifest)


def test_shared_unify_key_still_sourced_from_unity_secrets() -> None:
    manifest = build_unity_job_manifest(job_name="shared-key-staging")
    entry = _env_by_name(manifest)["SHARED_UNIFY_KEY"]
    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "unity-secrets",
        "key": "SHARED_UNIFY_KEY",
    }


def test_gcp_project_id_sourced_from_unity_config() -> None:
    manifest = build_unity_job_manifest(job_name="gcp-project-staging")
    entry = _env_by_name(manifest)["GCP_PROJECT_ID"]
    # Required key -> no "optional" flag.
    assert entry["valueFrom"]["configMapKeyRef"] == {
        "name": "unity-config",
        "key": "GCP_PROJECT_ID",
    }


def test_runtime_reconcile_mode_is_optional_unity_config_key() -> None:
    """``UNITY_DEPLOY_RUNTIME_RECONCILE_MODE`` is the one ConfigMap
    key that's allowed to be missing -- the assistant uses a default
    when it isn't set. Other ConfigMap-sourced keys are required.
    """
    manifest = build_unity_job_manifest(job_name="reconcile-mode-staging")
    entry = _env_by_name(manifest)["UNITY_DEPLOY_RUNTIME_RECONCILE_MODE"]
    config_ref = entry["valueFrom"]["configMapKeyRef"]
    assert config_ref["name"] == "unity-config"
    assert config_ref["key"] == "UNITY_DEPLOY_RUNTIME_RECONCILE_MODE"
    assert config_ref.get("optional") is True

    # And the other ConfigMap keys are NOT marked optional.
    for required_key in ("GCP_PROJECT_ID", "PROJECT_ID", "VERTEXAI_PROJECT"):
        ref = _env_by_name(manifest)[required_key]["valueFrom"]["configMapKeyRef"]
        assert ref.get("optional") is not True


def test_unity_startup_timing_is_disabled_literal_not_configmap_sourced() -> None:
    """Startup timing remains a manifest literal, not a ConfigMap key."""
    staging = build_unity_job_manifest(job_name="x", deploy_env="staging")
    production = build_unity_job_manifest(job_name="x", deploy_env="production")

    staging_entry = _env_by_name(staging)["UNITY_STARTUP_TIMING"]
    production_entry = _env_by_name(production)["UNITY_STARTUP_TIMING"]

    assert staging_entry == {"name": "UNITY_STARTUP_TIMING", "value": "0"}
    assert production_entry == {"name": "UNITY_STARTUP_TIMING", "value": "0"}


# ---------------------------------------------------------------------------
# extra_env overrides
# ---------------------------------------------------------------------------


def test_extra_env_overrides_default_service_urls_without_duplication() -> None:
    """Per-deploy ORCHESTRA_URL / UNITY_COMMS_URL / UNITY_ADAPTERS_URL
    overrides arrive via ``extra_env``. The override must replace the
    default (not duplicate it) so the container sees exactly one value
    per env name.
    """
    manifest = build_unity_job_manifest(
        job_name="unity-override-1207-staging",
        namespace="staging",
        deploy_env="staging",
        extra_env={
            "ORCHESTRA_URL": "https://internal.example.com/v0",
            "UNITY_COMMS_URL": "https://myslug---unity-comms-app-staging.run.app",
            "UNITY_ADAPTERS_URL": "https://myslug---unity-adapters-staging.run.app",
        },
    )

    env_by_name = _env_by_name(manifest)
    env_names = _env_names(manifest)

    assert env_names.count("ORCHESTRA_URL") == 1
    assert env_names.count("UNITY_COMMS_URL") == 1
    assert env_names.count("UNITY_ADAPTERS_URL") == 1

    assert env_by_name["ORCHESTRA_URL"]["value"] == (
        "https://internal.example.com/v0"
    )
    assert env_by_name["UNITY_COMMS_URL"]["value"] == (
        "https://myslug---unity-comms-app-staging.run.app"
    )
    assert env_by_name["UNITY_ADAPTERS_URL"]["value"] == (
        "https://myslug---unity-adapters-staging.run.app"
    )


# ---------------------------------------------------------------------------
# Image pull policy
# ---------------------------------------------------------------------------


def test_latest_image_tag_always_pulls() -> None:
    manifest = build_unity_job_manifest(
        job_name="unity-offline-latest-staging",
        namespace="staging",
        image="registry/unity-staging:latest",
    )
    assert _container(manifest)["imagePullPolicy"] == "Always"


def test_immutable_image_tag_uses_layer_cache() -> None:
    manifest = build_unity_job_manifest(
        job_name="unity-offline-sha-staging",
        namespace="staging",
        image="registry/unity-staging:4f25e7cbd9a4",
    )
    assert _container(manifest)["imagePullPolicy"] == "IfNotPresent"


def test_unity_image_hash_label_matches_image_tag() -> None:
    """The ``unity-image-hash`` label is what the AssistantSession
    controller filters on when claiming idle Jobs for the current
    build. It must equal the image tag exactly.
    """
    manifest = build_unity_job_manifest(
        job_name="image-hash-staging",
        image="registry/unity-staging:abc123def456",
    )
    assert manifest["metadata"]["labels"]["unity-image-hash"] == "abc123def456"


# ---------------------------------------------------------------------------
# Staging-only gateway transport opt-in (Phase A.bis soak)
# ---------------------------------------------------------------------------


def test_staging_deploy_env_activates_both_gateway_transports() -> None:
    """Staging Jobs must opt in to both ``unify.gateway`` transports.

    Pins the staging-soak configuration described in
    ``unity/gateway/PHASES.md`` (Phase A.bis). Activating these env
    vars routes both inbound Pub/Sub envelopes and outbound
    publisher.publish() calls through the newly-extracted transports,
    exposing any subtle regressions before the production cutover
    ever ships.
    """
    manifest = build_unity_job_manifest(
        job_name="gateway-transports-staging",
        namespace="staging",
        deploy_env="staging",
    )
    env_by_name = _env_by_name(manifest)
    assert env_by_name["UNITY_CONVERSATION_INGRESS_TRANSPORT"]["value"] == "pubsub"
    assert env_by_name["UNITY_CONVERSATION_OUTBOUND_TRANSPORT"]["value"] == "pubsub"


def test_production_deploy_env_does_not_activate_gateway_transports() -> None:
    """Production Jobs must NOT yet activate the new transports.

    Hosted production keeps the legacy inline subscribe_to_topic and
    inline publisher.publish paths until the staging soak (and
    Phase B / Phase C work) confirm the new transports are safe.
    This test guards against accidentally moving either env var out
    of the staging-only block before that point.
    """
    manifest = build_unity_job_manifest(
        job_name="gateway-transports-prod",
        namespace="production",
        deploy_env="production",
    )
    env_names = _env_names(manifest)
    assert "UNITY_CONVERSATION_INGRESS_TRANSPORT" not in env_names
    assert "UNITY_CONVERSATION_OUTBOUND_TRANSPORT" not in env_names


def test_no_legacy_staging_flag_in_any_environment() -> None:
    """DEPLOY_ENV is the single environment signal; the legacy STAGING
    boolean must never be injected into job manifests.
    """
    for deploy_env in ("staging", "production"):
        manifest = build_unity_job_manifest(job_name="x", deploy_env=deploy_env)
        assert "STAGING" not in _env_names(manifest)
        assert _env_by_name(manifest)["DEPLOY_ENV"]["value"] == deploy_env


# ---------------------------------------------------------------------------
# Pipeline artifact bucket name varies by deploy_env
# ---------------------------------------------------------------------------


def test_pipeline_artifact_bucket_production_uses_canonical_name() -> None:
    manifest = build_unity_job_manifest(job_name="x", deploy_env="production")
    bucket = _env_by_name(manifest)["UNITY_FILE_PIPELINE_ARTIFACT_BUCKET"]["value"]
    assert bucket == "unity-pipeline-artifacts"


def test_pipeline_artifact_bucket_staging_has_env_suffix() -> None:
    manifest = build_unity_job_manifest(job_name="x", deploy_env="staging")
    bucket = _env_by_name(manifest)["UNITY_FILE_PIPELINE_ARTIFACT_BUCKET"]["value"]
    assert bucket == "unity-pipeline-artifacts-staging"


# ---------------------------------------------------------------------------
# Top-level metadata + labels + spec
# ---------------------------------------------------------------------------


def test_default_priority_class_is_unity_idle() -> None:
    manifest = build_unity_job_manifest(job_name="x")
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["priorityClassName"] == "unity-idle"


def test_priority_class_override() -> None:
    manifest = build_unity_job_manifest(
        job_name="x",
        priority_class_name="unity-critical",
    )
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["priorityClassName"] == "unity-critical"


def test_extra_labels_merge_onto_job_metadata() -> None:
    manifest = build_unity_job_manifest(
        job_name="x",
        extra_labels={"assistant-session": "abc-123", "binding-id": "bind-xyz"},
    )
    labels = manifest["metadata"]["labels"]
    assert labels["assistant-session"] == "abc-123"
    assert labels["binding-id"] == "bind-xyz"
    # Defaults still present.
    assert labels["app"] == "unity"
    assert labels["unity-status"] == "idle"


def test_extra_annotations_appear_on_both_job_and_pod_template() -> None:
    manifest = build_unity_job_manifest(
        job_name="x",
        extra_annotations={"unify.ai/session-id": "ses-123"},
    )
    job_anns = manifest["metadata"]["annotations"]
    pod_anns = manifest["spec"]["template"]["metadata"]["annotations"]
    assert job_anns["unify.ai/session-id"] == "ses-123"
    assert pod_anns["unify.ai/session-id"] == "ses-123"
    # Pod gets the safe-to-evict guard regardless.
    assert pod_anns["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


def test_app_label_override_for_offline_and_dashboard_jobs() -> None:
    """`task_activation` and `dashboard_actions` use distinct
    ``app`` labels so the controller's idle-pool selector
    (``app=unity``) doesn't accidentally pick up those one-shot Jobs.
    """
    offline = build_unity_job_manifest(job_name="x", app_label="unity-offline")
    dashboard = build_unity_job_manifest(
        job_name="x",
        app_label="unity-dashboard-action",
    )
    assert offline["metadata"]["labels"]["app"] == "unity-offline"
    assert offline["spec"]["template"]["metadata"]["labels"]["app"] == "unity-offline"
    assert dashboard["metadata"]["labels"]["app"] == "unity-dashboard-action"


def test_ttl_and_active_deadline_only_appear_when_set() -> None:
    bare = build_unity_job_manifest(job_name="x")
    assert "ttlSecondsAfterFinished" not in bare["spec"]
    assert "activeDeadlineSeconds" not in bare["spec"]

    bounded = build_unity_job_manifest(
        job_name="x",
        ttl_seconds_after_finished=300,
        active_deadline_seconds=3600,
    )
    assert bounded["spec"]["ttlSecondsAfterFinished"] == 300
    assert bounded["spec"]["activeDeadlineSeconds"] == 3600


def test_container_resources_are_right_sized() -> None:
    """Pins the 2 vCPU / 8 GiB / 10 GiB ephemeral shape applied in
    the 2026-04 rightsizing. Tests against accidental regression to
    the previous 2 vCPU / 16 GiB / 100 GiB shape (which was actually
    billed as 2.46 vCPU on Autopilot's 1:6.5 ratio).
    """
    resources = _container(build_unity_job_manifest(job_name="x"))["resources"]
    expected = {
        "cpu": "2",
        "memory": "8Gi",
        "ephemeral-storage": "10Gi",
    }
    assert resources["requests"] == expected
    assert resources["limits"] == expected


def test_job_top_level_shape() -> None:
    manifest = build_unity_job_manifest(job_name="shape-staging", namespace="staging")
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == "shape-staging"
    assert manifest["metadata"]["namespace"] == "staging"
    assert manifest["spec"]["backoffLimit"] == 0
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    # Assistant Jobs run under the minimally-scoped Workload Identity SA, not the
    # fleet-wide comm-sa, and mount no JSON service-account key.
    assert pod_spec["serviceAccountName"] == "assistant-runtime-sa"
    volume_names = {v["name"] for v in pod_spec.get("volumes", [])}
    assert "sa-key" not in volume_names
    container = pod_spec["containers"][0]
    mount_names = {m["name"] for m in container.get("volumeMounts", [])}
    assert "sa-key" not in mount_names
    env_names = {e["name"] for e in container["env"]}
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env_names


# ---------------------------------------------------------------------------
# Submission wrapper: `create_unity_job` calls the K8s API exactly once
# ---------------------------------------------------------------------------


class _FakeBatchApi:
    """Minimal Batch API double for testing the submission wrapper.

    Records the namespace + body it was called with and lets each
    test inject the response or exception it wants.
    """

    def __init__(self, *, response=None, exception=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._response = response
        self._exception = exception

    def create_namespaced_job(self, namespace, body):
        self.calls.append((namespace, body))
        if self._exception is not None:
            raise self._exception
        return self._response or SimpleNamespace(
            metadata=SimpleNamespace(name=body["metadata"]["name"], uid="uid-1"),
        )


def test_create_unity_job_submits_built_manifest_to_kube_api_once() -> None:
    """The wrapper passes whatever ``build_unity_job_manifest`` produced
    to ``create_namespaced_job``, with the right namespace, exactly
    one time.
    """
    batch_api = _FakeBatchApi()
    response = create_unity_job(
        batch_api,
        job_name="submit-once-staging",
        namespace="staging",
        deploy_env="staging",
    )

    assert len(batch_api.calls) == 1
    submitted_namespace, submitted_body = batch_api.calls[0]
    assert submitted_namespace == "staging"
    # The submitted body equals what build_unity_job_manifest would
    # produce for the same args -- pins that the wrapper doesn't
    # rewrite the manifest between build and submit.
    expected_body = build_unity_job_manifest(
        job_name="submit-once-staging",
        namespace="staging",
        deploy_env="staging",
    )
    assert submitted_body == expected_body
    assert response.metadata.name == "submit-once-staging"


def test_create_unity_job_swallows_409_conflict_and_returns_none() -> None:
    """The idle-pool replenish path races itself by design. A 409
    Conflict from the K8s API means another worker already created
    the Job; we must return None rather than raising so the racing
    workers don't error out.
    """
    from kubernetes.client.rest import ApiException

    batch_api = _FakeBatchApi(exception=ApiException(status=409, reason="Conflict"))
    result = create_unity_job(
        batch_api,
        job_name="conflict-test-staging",
        namespace="staging",
    )
    assert result is None
    assert len(batch_api.calls) == 1  # We did attempt the submit before 409


def test_create_unity_job_returns_none_on_other_api_exceptions() -> None:
    """Non-409 K8s API failures (5xx, auth, etc.) are surfaced via
    return-None + printed message rather than raising. Production
    callers depend on this swallow-and-return-None contract -- if
    you change it, audit every caller of create_unity_job first.
    """
    from kubernetes.client.rest import ApiException

    batch_api = _FakeBatchApi(exception=ApiException(status=500, reason="Server"))
    result = create_unity_job(
        batch_api,
        job_name="server-error-test-staging",
        namespace="staging",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Cloudbuild env-var contract (cross-file invariant; no manifest builder
# involved, but lives alongside manifest tests because the values flow
# directly into the manifest env vars)
# ---------------------------------------------------------------------------


def test_comms_staging_deploy_resets_runtime_service_urls() -> None:
    text = (ROOT / "cloudbuild/unity-comms-app-staging.yaml").read_text()

    canonical_runtime_env = (
        "--update-env-vars=DEPLOY_ENV=staging,"
        "ORCHESTRA_URL=${_ORCHESTRA_URL},"
        "UNITY_COMMS_URL=https://${_COMMS_HOST},"
        "UNITY_ADAPTERS_URL=https://${_ADAPTERS_HOST}"
    )

    assert canonical_runtime_env in text
    assert "_ORCHESTRA_URL: 'https://internal.example.com/v0'" in text
    assert "_COMMS_HOST: 'service.a.run.app'" in text
    assert "_ADAPTERS_HOST: 'service.a.run.app'" in text


def test_adapters_deploys_do_not_retype_secret_backed_urls() -> None:
    """Adapter services keep their public URL env vars secret-backed in Cloud Run.

    Setting those names as literals in the deploy command makes Cloud Run reject
    the revision because the env var already exists with a different type.
    """
    staging_text = (ROOT / "cloudbuild/adapters-staging.yaml").read_text()
    production_text = (ROOT / "cloudbuild/adapters.yaml").read_text()

    assert "UNITY_ADAPTERS_URL=https://" not in staging_text
    assert "UNITY_ADAPTERS_URL=https://" not in production_text
    assert "UNITY_COMMS_URL=https://" not in production_text
