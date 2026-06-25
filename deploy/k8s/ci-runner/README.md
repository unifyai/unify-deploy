# CI integration runner (Integration smoke merge gate)

A self-hosted GitHub Actions runner that lives **inside the `unity` GKE cluster**
(namespace `staging`) and executes `.github/workflows/integration-smoke.yml`.

It must run in-cluster because the merge-gate tests call internal Orchestra
(`https://internal.example.com`) and the private-node cluster API
server — neither is reachable from a GitHub-hosted runner.

## What runs here

`pytest -m merge_gate tests/communication/infra/integration` — the minimal
representative subset that proves the core staging product is healthy before a
`staging -> main` promotion: pool health, wakeup → container, scheduled +
offline task firing, VM assignment, cleanup/maintenance safety, and full
delete-time teardown. Each test cleans up after itself; the workflow adds a
created-assistant reaper and a staging reconcile pass so runs stay leak-neutral.

## One-time setup

1. **GitHub registration token.** Create a PAT (or GitHub App installation
   token) with `repo` + `admin:org`/`admin:repo_hook` runner-registration scope
   and store it in Secret Manager:

   ```bash
   gcloud secrets create CI_RUNNER_GITHUB_TOKEN --project=gcp-project-runtime
   printf '%s' "<PAT>" | gcloud secrets versions add CI_RUNNER_GITHUB_TOKEN \
     --data-file=- --project=gcp-project-runtime
   ```

   The `external-secrets` GCP credential must be able to read it (same
   ClusterSecretStore used by `deploy/k8s/secrets/`).

2. **GCP access.** The runner mounts the existing `comm-sa-key` Secret (the
   `comm-sa@gcp-project-runtime` JSON key) for GCE pool VMs + Pub/Sub. No
   new GCP SA is required; `comm-sa` already has the needed cross-project GCE and
   Pub/Sub roles.

3. **Dedicated test assistant.** Create one long-lived **deployed**
   (`is_local=false`), **non-desktop** staging assistant that the wakeup/VM
   tests reuse, and set its id as a repo variable:

   ```bash
   gh variable set TEST_ASSISTANT_ID --repo unifyai/unity-deploy --body "<agent_id>"
   ```

4. **Repo secrets used by the workflow** (GitHub injects these into the job):
   `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, `CLONE_TOKEN`, `UNITY_COMMS_URL`,
   `UNITY_ADAPTERS_URL` (all already used by `hosted-tests.yml`).

5. **Build & push the runner image**, then apply the manifests:

   ```bash
   docker build -f deploy/k8s/ci-runner/Dockerfile \
     -t us-central1-docker.pkg.dev/gcp-project-runtime/unity-comms-app-repo/ci-integration-runner:latest \
     deploy/k8s/ci-runner
   docker push us-central1-docker.pkg.dev/gcp-project-runtime/unity-comms-app-repo/ci-integration-runner:latest

   kubectl apply -f deploy/k8s/ci-runner/rbac.yaml
   kubectl apply -f deploy/k8s/ci-runner/external-secret.yaml
   kubectl apply -f deploy/k8s/ci-runner/deployment.yaml
   ```

6. Confirm the runner registered: GitHub → repo → Settings → Actions → Runners
   shows a runner with labels `self-hosted, unity-cluster, staging`.

## Required status check

`Integration smoke` is configured as a required status check on `main`, so a
`staging -> main` PR cannot merge until the gate passes. On unrelated PRs the
job's `if:` condition makes it skip (which branch protection treats as passing).

## Cost / cleanup model

- Every `merge_gate` test releases the resources it creates in a `finally` block.
- The integration `conftest` records created assistants and reaps survivors at
  session end (`pytest_sessionfinish`).
- The workflow writes created assistant ids to `CI_CREATED_ASSISTANTS_FILE` and,
  in `always()` steps, deletes any leftovers and fires the staging reconcile
  endpoints (`/scheduled/jobs/cleanup`, `/scheduled/infra/maintenance`). This
  covers even a hard-killed run, so the gate does not accumulate paid resources.
