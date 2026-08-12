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


def test_openrouter_api_key_is_not_mounted_into_the_pod() -> None:
    """The pod brokers OpenRouter, so it holds no OpenRouter credential.

    Scrubbing the key from sandboxes did not contain it: the default
    execute_code surface runs in-process, inside the trusted parent, with
    os.environ intact and the real ``unillm`` module in scope. Keeping the
    key out of the pod is what makes that surface uninteresting to reach.
    """
    assert "OPENROUTER_API_KEY" not in _env_by_name(
        build_unity_job_manifest(job_name="openrouter-key-staging"),
    )


def test_llm_gateway_points_at_the_sidecar_over_loopback() -> None:
    """The generation goes pod -> provider; only metering leaves the pod.

    Previously this tracked ORCHESTRA_URL, which sent the bytes themselves
    through a service that serves 400 concurrent requests in total -- one
    streamed call held a slot for its whole duration, and every voice turn
    paid the round trip. Loopback reaches the sidecar beside the runtime,
    which holds the provider keys and calls Orchestra only to authorise and
    to report what was spent.
    """
    env = _env_by_name(build_unity_job_manifest(job_name="llm-gateway-staging"))

    assert env["UNILLM_LLM_GATEWAY_URL"]["value"] == "http://127.0.0.1:8787/llm"


def test_the_route_out_replaces_the_key_rather_than_accompanying_it() -> None:
    """Removing the key is only safe while the broker route is configured.

    These two move together and in this order: routing arrives first and is
    verified, then the key goes. A manifest carrying neither strands every
    OpenRouter call with no way to make it — the platform default model
    routes through OpenRouter, so that is total inference loss, and it
    would read as an unrelated env cleanup rather than the outage it is.
    """
    env = _env_by_name(build_unity_job_manifest(job_name="gateway-swap-staging"))

    assert "UNILLM_LLM_GATEWAY_URL" in env
    assert "OPENROUTER_API_KEY" not in env


def test_no_provider_billing_key_is_mounted_into_the_pod() -> None:
    """Both providers are brokered, so the pod holds neither credential.

    Named individually rather than checked as a set: each was removed only
    once its own leg was proven end to end, and a future provider added to
    the mount list should fail here until the same is true of it.
    """
    env = _env_by_name(build_unity_job_manifest(job_name="provider-keys-staging"))

    assert "OPENROUTER_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_eventbus_orchestra_persist_allowlist_on_assistant_jobs() -> None:
    """CM and offline Jobs inherit scoped Orchestra EventBus persistence.

    Publishing + Pub/Sub stay on for Live Actions; Orchestra Events/* is
    narrowed to the CodeAct root plus execute_code / execute_function.
    """
    manifest = build_unity_job_manifest(job_name="eventbus-persist-staging")
    env = _env_by_name(manifest)
    assert env["EVENTBUS_PUBLISHING_ENABLED"]["value"] == "true"
    assert env["EVENTBUS_PUBSUB_STREAMING"]["value"] == "true"
    assert env["EVENTBUS_ORCHESTRA_PERSIST_MODE"]["value"] == "allowlist"
    assert (
        env["EVENTBUS_ORCHESTRA_PERSIST_TOOLS"]["value"]
        == "act,execute_code,execute_function"
    )


def test_orchestra_admin_key_not_mounted_on_assistant_pods() -> None:
    """The platform admin key must never reach an assistant Job pod.

    Pods authenticate to Orchestra and the hosted gateway with their own
    per-assistant UNIFY_KEY against ownership-scoped routes; the fleet-wide
    ORCHESTRA_ADMIN_KEY stays on controllers / Cloud Run / reconcile jobs only.
    """
    manifest = build_unity_job_manifest(job_name="orch-admin-key-staging")
    assert "ORCHESTRA_ADMIN_KEY" not in _env_names(manifest)


def test_shared_unify_key_not_mounted_on_assistant_pods() -> None:
    """Fleet audit auth is system-project + admin key on comms, not a pod env key."""
    manifest = build_unity_job_manifest(job_name="shared-key-staging")
    assert "SHARED_UNIFY_KEY" not in _env_names(manifest)


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
# Google Meet signed-in browser session
# ---------------------------------------------------------------------------


def test_meet_bridge_page_url_is_served_by_comms() -> None:
    """The Recall bot loads this page, so it must be the public comms host.

    A bot renders the page from Recall's cloud; a cluster-internal or
    pod-local URL would resolve to nothing and the bot would join mute.
    """
    manifest = build_unity_job_manifest(job_name="x", deploy_env="staging")
    env = _env_by_name(manifest)
    comms_url = env["UNITY_COMMS_URL"]["value"].rstrip("/")
    assert env["MEET_BRIDGE_PAGE_URL"]["value"] == f"{comms_url}/meet/bridge"


def test_recall_relay_secret_reaches_the_pod() -> None:
    """The pod builds the relay URL, so it needs the shared secret itself.

    Without it the provider registers no realtime endpoint at all: the bot is
    never told where to push participant events, and the assistant receives no
    inbound chat, no speaker attribution and no roster -- silently, because a
    missing endpoint is indistinguishable from a quiet meeting.
    """
    env = _env_by_name(build_unity_job_manifest(job_name="x"))
    ref = env["RECALL_RELAY_SECRET"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "unity-secrets"
    assert ref["key"] == "RECALL_RELAY_SECRET"
    # Optional for the same reason as the API key: an unprovisioned environment
    # must still boot.
    assert ref["optional"] is True


def test_recall_region_travels_in_the_manifest() -> None:
    """A key is valid in exactly one Recall deployment.

    The pod would otherwise fall back to a code default, so a region mismatch
    would surface as an authentication failure with nothing in the manifest to
    point at it.
    """
    for deploy_env in ("staging", "production"):
        manifest = build_unity_job_manifest(job_name="x", deploy_env=deploy_env)
        assert _env_by_name(manifest)["RECALL_REGION"]["value"] == "eu-central-1"


def test_recall_api_key_is_an_optional_secret_key() -> None:
    """An environment with no Recall account provisioned must still boot.

    Same contract as the Meet twin credentials: absent key means the pod comes
    up and stays on the agent-service provider rather than failing to start.
    """
    env = _env_by_name(build_unity_job_manifest(job_name="recall-staging"))
    secret_ref = env["RECALL_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == "unity-secrets"
    assert secret_ref["key"] == "RECALL_API_KEY"
    assert secret_ref["optional"] is True


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


def test_app_label_override_for_offline_jobs() -> None:
    """`task_execution` overrides the ``app`` label so the controller's
    idle-pool selector (``app=unity``) doesn't accidentally pick up those
    one-shot Jobs.
    """
    offline = build_unity_job_manifest(job_name="x", app_label="unity-offline")
    assert offline["metadata"]["labels"]["app"] == "unity-offline"
    assert offline["spec"]["template"]["metadata"]["labels"]["app"] == "unity-offline"


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


def test_termination_grace_period_override() -> None:
    defaulted = build_unity_job_manifest(job_name="x")
    assert defaulted["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 30
    longer = build_unity_job_manifest(
        job_name="x",
        termination_grace_period_seconds=120,
    )
    assert longer["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 120


def _millicores(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def _mebibytes(value: str) -> int:
    return int(value[:-2]) * (1024 if value.endswith("Gi") else 1)


def test_pod_totals_the_right_sized_allocation() -> None:
    """Pins what Autopilot actually bills: the sum across containers.

    Autopilot charges a pod for the resources it requests, so the total is
    the cost and any one container's share is an implementation detail. The
    2 vCPU / 8 GiB / 10 GiB shape is the 2026-04 rightsizing; the earlier
    2 vCPU / 16 GiB was billed as 2.46 vCPU under Autopilot's 1:6.5 ratio,
    which is the regression this guards against.

    Adding the broker sidecar deliberately did not move this number -- its
    request came out of the runtime's headroom rather than onto the bill.
    """
    containers = build_unity_job_manifest(job_name="x")["spec"]["template"]["spec"][
        "containers"
    ]

    for field in ("requests", "limits"):
        cpu = sum(_millicores(c["resources"][field]["cpu"]) for c in containers)
        memory = sum(_mebibytes(c["resources"][field]["memory"]) for c in containers)
        storage = sum(
            _mebibytes(c["resources"][field]["ephemeral-storage"]) for c in containers
        )
        assert cpu == 2000, f"{field} cpu"
        assert memory == 8 * 1024, f"{field} memory"
        assert storage == 10 * 1024, f"{field} ephemeral-storage"


def test_job_top_level_shape() -> None:
    manifest = build_unity_job_manifest(job_name="shape-staging", namespace="staging")
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == "shape-staging"
    assert manifest["metadata"]["namespace"] == "staging"
    assert manifest["spec"]["backoffLimit"] == 0
    assert (
        build_unity_job_manifest(job_name="x", backoff_limit=2)["spec"]["backoffLimit"]
        == 2
    )
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["terminationGracePeriodSeconds"] == 30
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


def _deploy_step_args(filename: str, step_id: str) -> str:
    """The shell body of one Cloud Build step, by id.

    Scoped per step because the staging and production deploys were
    consolidated into a single orchestrator (``deploy/cloudbuild*.yaml``): a
    whole-file grep now spans every service, so an assertion about one of them
    would match another's arguments.
    """
    import yaml

    doc = yaml.safe_load((ROOT / filename).read_text())
    for step in doc["steps"]:
        if step.get("id") == step_id:
            return "\n".join(step.get("args") or [])
    raise AssertionError(f"no step {step_id!r} in {filename}")


def test_comms_staging_deploy_resets_runtime_service_urls() -> None:
    """The comms revision must carry the staging service URLs, not inherit stale ones."""
    body = _deploy_step_args("deploy/cloudbuild-staging.yaml", "deploy-comms")
    for assignment in (
        "DEPLOY_ENV=staging",
        "ORCHESTRA_URL=${_ORCHESTRA_URL}",
        "UNITY_COMMS_URL=https://${_COMMS_HOST}",
        "UNITY_ADAPTERS_URL=https://${_ADAPTERS_HOST}",
    ):
        assert assignment in body, assignment

    text = (ROOT / "deploy/cloudbuild-staging.yaml").read_text()
    assert "_ORCHESTRA_URL: 'https://internal.example.com/v0'" in text
    assert "_COMMS_HOST: 'service.a.run.app'" in text
    assert "_ADAPTERS_HOST: 'service.a.run.app'" in text


def test_adapters_deploys_do_not_retype_secret_backed_urls() -> None:
    """A URL cannot be a literal on a service where it is secret-backed.

    Cloud Run rejects the revision when an env var already exists with a
    different type. ``UNITY_ADAPTERS_URL`` is secret-backed on the adapters
    service, so it must never appear as a literal there. ``UNITY_COMMS_URL`` is
    not, so setting it literally is correct -- which is why this asserts against
    the secret list rather than banning both names outright.
    """
    for filename in ("deploy/cloudbuild-staging.yaml", "deploy/cloudbuild.yaml"):
        body = _deploy_step_args(filename, "deploy-adapters")
        secret_backed = {
            entry.split("=", 1)[0]
            for chunk in body.split("--set-secrets=")[1:]
            for entry in chunk.split(" ")[0].split(",")
            if "=" in entry
        }
        for name in secret_backed:
            assert f"{name}=https://" not in body, f"{name} retyped in {filename}"
        assert "UNITY_ADAPTERS_URL" in secret_backed, filename


def test_workflows_dir_is_not_stamped_from_this_image() -> None:
    """A path resolved here is a fact about the comms image, not the Job's.

    UNITY_WORKFLOWS_DIR used to be set from this process's own installed
    unify_deploy, which lives at /app in the comms image and under
    site-packages in the assistant image. The Job then received an absolute
    path that did not exist in its own container: the assistant logged
    "Workflow catalogue root /app/unify_deploy/... does not exist",
    registered no bundles, and every workflow install reported an empty
    catalogue while the package sat two directories away.

    The catalogue ships inside the runtime image, so the runtime resolves
    it. The variable remains an explicit override for self-host and
    development, set in the same container that reads it.
    """
    manifest = build_unity_job_manifest(job_name="workflows-dir-staging")
    assert "UNITY_WORKFLOWS_DIR" not in _env_by_name(manifest)


# ---------------------------------------------------------------------------
# Provider-key broker sidecar
# ---------------------------------------------------------------------------


def _sidecar(manifest: dict) -> dict:
    containers = manifest["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "llm-broker")


def test_process_namespace_is_not_shared_with_the_sidecar() -> None:
    """This flag is the boundary; without it the sidecar is decorative.

    Sharing the namespace would put the broker's /proc — and the provider
    credentials in its environment — back within reach of code running in
    the runtime container, which is the entire thing it exists to prevent.
    Kubernetes defaults it to false, but a default is not a decision and
    flipping it would fail silently.
    """
    spec = build_unity_job_manifest(job_name="ns-staging")["spec"]["template"]["spec"]
    assert spec["shareProcessNamespace"] is False


def test_the_sidecar_runs_the_runtime_image_under_its_own_command() -> None:
    """A second image would need its own build and could drift from the runtime."""
    manifest = build_unity_job_manifest(job_name="sidecar-image-staging")
    sidecar = _sidecar(manifest)
    assert sidecar["image"] == _container(manifest)["image"]
    assert sidecar["command"] == ["python", "-m", "unify.llm_broker"]


def test_the_sidecar_carries_the_provider_credentials() -> None:
    env = {
        e["name"]: e for e in _sidecar(build_unity_job_manifest(job_name="k"))["env"]
    }
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        assert env[key]["valueFrom"]["secretKeyRef"]["key"] == key


def test_the_sidecar_environment_stays_minimal() -> None:
    """Every extra variable shares a process with the keys.

    The sidecar needs credentials and somewhere to report spend. Anything
    else widens what a compromise of it would yield, for no benefit.
    """
    env_names = {
        e["name"] for e in _sidecar(build_unity_job_manifest(job_name="k"))["env"]
    }
    assert env_names == {"ORCHESTRA_URL", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"}


def test_the_sidecar_is_not_given_the_pods_own_identity() -> None:
    """It brokers calls; it is not a second copy of the assistant.

    UNIFY_KEY is the runtime's identity and is what Orchestra meters
    against. The broker is handed one per request by its caller, so holding
    a standing copy would only add a credential to the blast radius.
    """
    env_names = {
        e["name"] for e in _sidecar(build_unity_job_manifest(job_name="k"))["env"]
    }
    assert "UNIFY_KEY" not in env_names


def test_the_sidecar_reports_whether_it_is_actually_serving() -> None:
    """restartPolicy is Never, so a dead broker cannot recover -- only report.

    The probe must exec inside the container. An httpGet probe is run by the
    kubelet from the node and reaches a container by pod IP, so it cannot see
    the broker's loopback-bound listener and fails however healthy the broker
    is -- staging showed exactly that. Binding wider to satisfy it would
    publish the broker on a cluster-routable address, which is the one thing
    this container must not do.
    """
    probe = _sidecar(build_unity_job_manifest(job_name="probe-staging"))[
        "readinessProbe"
    ]

    assert "httpGet" not in probe
    assert "127.0.0.1:8787/healthz" in " ".join(probe["exec"]["command"])
