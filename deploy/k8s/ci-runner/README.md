# CI integration runner (Integration smoke merge gate)

A self-hosted GitHub Actions runner that lives **inside the `unity` GKE cluster**
(namespace `staging`) and executes the `merge-gate` job of both integration-smoke
workflows:

- `.github/workflows/integration-smoke-release-gate.yml` — the `staging -> main`
  release gate. Triggers on `pull_request` against `main` and
  `workflow_dispatch` only; its aggregator job publishes the required
  `Integration smoke` status check.
- `.github/workflows/integration-smoke.yml` — the everyday, non-gating ad-hoc
  runner. Triggers on `workflow_dispatch` and on a push whose commit message
  carries the `[run-integration]` tag. It publishes no required context.

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

1. **GitHub App registration credential.** Registration authenticates as an
   **org-owned GitHub App**, not a user PAT. This is deliberate: a PAT belongs
   to whoever created it, so it dies with that person's account or credential
   rotation, and it carries a hard expiry that silently takes the merge gate
   down (see "Why an App, not a PAT" below).

   Create the App under the **`unifyai` org** (Settings → Developer settings →
   GitHub Apps → New GitHub App):

   - Repository permissions **Administration: Read and write** and
     **Metadata: Read-only** — the pair Actions Runner Controller documents
     for repo-level runner registration, and all it needs. GitHub selects
     Metadata automatically once any repository permission is set. Nothing
     else; in particular it needs no code, contents, or org-level access.
     (An *org*-level runner would instead need **Self-hosted runners: Read
     and write**.)
   - Uncheck **Webhook → Active**.
   - **Only on this account** for installation scope.
   - After creating it, **Generate a private key** (downloads a `.pem`), then
     **Install App** onto `unifyai/unify-deploy` only.

   Store the private key in Secret Manager and set the App ID in
   `deployment.yaml` (`APP_ID` — not secret):

   ```bash
   gcloud secrets create CI_RUNNER_GITHUB_APP_PRIVATE_KEY \
     --project=gcp-project-runtime
   gcloud secrets versions add CI_RUNNER_GITHUB_APP_PRIVATE_KEY \
     --data-file=<app-private-key>.pem --project=gcp-project-runtime
   ```

   `external-secrets-reader@gcp-project-runtime` holds project-level
   `secretAccessor`, so no per-secret IAM grant is required.

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
   `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, `CLONE_TOKEN`, `UNIFY_COMMS_URL`,
   `UNIFY_ADAPTERS_URL` (all already used by `hosted-tests.yml`).

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

## Why an App, not a PAT

The runner's credential is the single point of failure for the whole merge
gate, and a PAT fails in the two ways that are hardest to diagnose:

- **It expires.** When it does, the runner stops accepting jobs, the required
  `Integration smoke` context is never published, and every `staging -> main`
  release PR blocks on a check that just sits pending. Nothing reports an
  authentication error, so the symptom looks nothing like an expired
  credential.
- **It belongs to a person.** Shared CI infrastructure that depends on one
  engineer's account breaks when they leave or rotate their credentials.

An org-owned App fixes both: it is owned by `unifyai` rather than an
individual, and its private key has no expiry. The entrypoint exchanges the
key for a short-lived installation token on **every** registration, so the
token's 1h lifetime is not a constraint even though this runner is ephemeral
and re-registers after each job.

`APP_ID`/`APP_LOGIN`/`APP_PRIVATE_KEY` are mutually exclusive with
`ACCESS_TOKEN`/`RUNNER_TOKEN`; setting both styles makes the entrypoint exit.

## Required status check

`Integration smoke` is configured as a required status check on `main`, so a
`staging -> main` PR cannot merge until the gate passes. The context is
published by the `integration-smoke-required` aggregator job in
`integration-smoke-release-gate.yml`, which runs `if: always()` and **fails**
unless the `merge-gate` job it depends on reported `success`. A skipped or
failed run is therefore an explicit red, never an implicit pass.

The gate is fail-closed because that workflow file has **no `push` trigger at
all** — only `pull_request: branches: [main]` and `workflow_dispatch`. An
ordinary push produces no check run under this context whatsoever, so the
required check stays genuinely `pending` until the real PR-triggered run answers
it.

Scoping the aggregator job's own `if:` instead is **not** sufficient and must not
be reintroduced: GitHub Actions still publishes a *skipped* check run for a job
whose `if:` evaluates false, and branch protection treats a skipped required
check as satisfied — which is how unify-deploy#128 merged on a stale pass. For
the same reason the ad-hoc workflow's test job is named `Integration smoke (run)`,
distinct from the required `Integration smoke` context; a job name that collides
with the required context recreates the same bug from a different file. See
AGENTS.md § "Staging→Main Release Gates Are Fail-Closed".

## Cost / cleanup model

- Every `merge_gate` test releases the resources it creates in a `finally` block.
- The integration `conftest` records created assistants and reaps survivors at
  session end (`pytest_sessionfinish`).
- The workflow writes created assistant ids to `CI_CREATED_ASSISTANTS_FILE` and,
  in `always()` steps, deletes any leftovers and fires the staging reconcile
  endpoints (`/scheduled/jobs/cleanup`, `/scheduled/infra/maintenance`). This
  covers even a hard-killed run, so the gate does not accumulate paid resources.
