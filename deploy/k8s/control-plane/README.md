# Control-Plane Reconciliation Job

Unity deploys reconcile durable control-plane metadata into Orchestra with a
one-off Kubernetes Job. This keeps metadata such as assistant
`console_config` available before Console is opened and before a Unity
assistant wakes.

## Deploy Flow

The permanent deploy path is:

1. Cloud Build builds and pushes the SHA-tagged Unity image.
2. Cloud Build creates a `unity-control-plane-reconcile-*` Job in the target
   namespace.
3. The Job runs the just-built image with `ORCHESTRA_ADMIN_KEY` from the
   namespace-local `unity-secrets`.
4. Cloud Build waits for the Job, prints its logs, and deletes it.
5. Idle Unity jobs are refreshed only after reconciliation succeeds.

Pipeline workers may still refresh in parallel because they do not wake
assistants through Console.

## Required Secret

Each target namespace must contain `unity-secrets` with:

- `ORCHESTRA_ADMIN_KEY`

The Job intentionally reads the admin key from Kubernetes rather than from
Cloud Build or a developer shell.

## Cloud Build Wiring

Staging is wired in `deploy/cloudbuild-staging.yaml` and runs:

```bash
bash deploy/scripts/run_control_plane_reconcile_job.sh \
  --environment staging \
  --namespace staging \
  --image "${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_REPOSITORY}/unity-staging:${_UNITY_SHA}" \
  --orchestra-url "https://internal.example.com/v0" \
  --timeout 300s
```

Production is wired in `deploy/cloudbuild.yaml` and runs:

```bash
bash deploy/scripts/run_control_plane_reconcile_job.sh \
  --environment production \
  --namespace production \
  --image "${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_REPOSITORY}/unity:${SHORT_SHA}" \
  --orchestra-url "https://api.unify.ai/v0" \
  --timeout 300s
```

The runner validates the environment and namespace pair before creating the
Job, then the reconciler validates that `ORCHESTRA_URL` matches the requested
environment from inside the container.

## Extending Artifacts

The Job is intentionally artifact-agnostic: it runs
`unity_deploy.scripts.reconcile_control_plane` for the whole target
environment. New deploy-time control-plane artifacts should be added to the
code registry, not to the Kubernetes Job.

The usual path is:

1. Add the desired artifact field or model to the deployment declaration layer,
   such as `DeploymentSpec` or a dedicated control-plane spec.
2. Declare the artifact in each client deployment that owns it.
3. Extend `unity_deploy.control_plane.reconcile.build_control_plane_plan()` to
   translate registered declarations and `DeploymentTarget` mappings into
   `ReconcileOperation` writes.
4. Add tests for planning, filtering, clearing stale state, and CLI behavior.

For assistant-scoped artifacts, the assistant IDs come from
`DeploymentTarget(scope="assistant", scope_id=...)`. Normal deploys should not
pass `--assistant-id`; that flag is only for targeted manual recovery.

## Manual Break-Glass Run

Use the runner directly after fetching cluster credentials. Prefer a full
environment reconciliation; only pass `--client` or `--assistant-id` when
recovering a specific target.

```bash
gcloud container clusters get-credentials unity --region us-central1

bash deploy/scripts/run_control_plane_reconcile_job.sh \
  --environment staging \
  --namespace staging \
  --image us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-staging:<sha> \
  --orchestra-url https://internal.example.com/v0
```

```bash
gcloud container clusters get-credentials unity --region us-central1

bash deploy/scripts/run_control_plane_reconcile_job.sh \
  --environment production \
  --namespace production \
  --image us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:<sha> \
  --orchestra-url https://api.unify.ai/v0
```
