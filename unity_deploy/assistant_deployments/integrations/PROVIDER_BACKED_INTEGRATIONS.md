# Provider-Backed Integrations Boundary

`unity-deploy` still owns full integration packages under `integrations/packages/*` and `integrations/client_packages/*`. These packages are the right choice when Unify needs Python code, DataManager sync, custom guidance, client-specific logic, browser fallback, or deterministic tests.

Dynamic provider-backed apps are different. Composio, Pipedream, first-party OAuth backends, and custom provider SDKs are registered in Orchestra and surfaced to Unity as `primitives.integrations.<app>.<tool>` virtual FunctionManager records. They do not require a folder in this repository for every supported app.

Use the three-level rule:

- Level 1 dynamic provider pass-through: provider-supported app, generic actions are good enough, no local package.
- Level 2 Unify overlay: provider-supported app with curated display text, scopes, capability groups, action policy, or actor guidance, still no Python package.
- Level 3 full package: custom runtime code, local sync, client workflow, strict auth behavior, unsupported app, or high-control integration.

Prefer Level 1/2 whenever a provider-backed app already satisfies the product need. A Level 3 package is intentionally the exception for custom runtime code, deterministic local sync, provider gaps, client-specific workflows, or stricter control requirements; it should not be the default for every app Composio or Pipedream can already cover.

`IntegrationManifest` remains the contract for Level 3 full packages only. It must not become the required contract for every Composio/Pipedream app.

`Integrations/Manifests` is deploy-time telemetry and native deployment state.
Runtime connection state for provider-backed apps lives in Orchestra's
integration connection registry. Static package enablement in Unity is based on
disk package discovery plus local SecretManager key presence.

`DeploymentSpec.integrations` and seed-layer `integrations=[...]` are disk
package slugs only. They should resolve to a discovered Level 3 package under
`packages/`, `client_packages/`, an opt-in `mock_packages/` root, an entry
point, or an explicit search path. They are not the provider catalog and must
not be populated with every Composio or Pipedream app slug.

Console and Orchestra own the dynamic provider lifecycle:

- Provider backend status, catalog sync, OAuth/connect sessions, and
  connected-account IDs live in Orchestra. Provider credentials and endpoint
  overrides live in deployment environment variables, not admin API payloads.
- Builtins project contexts are the durable app/tool catalog:
  `Builtins/Integrations/Apps`, `Builtins/Integrations/Tools`, and
  `Builtins/Integrations/Meta`. The Orchestra DB app/tool catalog tables are
  compatibility projections only; connection, auth, policy, approval, and audit
  rows remain active Orchestra operational state.
- Hosted Cloud bootstrap executes one generic Builtins seed Cloud Run Job per
  environment. The Unity job seeds Builtins functions/guidance and provider
  integration catalog artifacts directly into Builtins logging contexts.
  Integration artifacts write keyed app/tool log rows and store per-batch
  checkpoints in `Integrations/Meta`.
- Console presents the gallery, permission review, one-click connect, API key
  entry, reconnect, and disconnect flows.
- Unity reads Builtins catalog artifacts and exposes searchable virtual tools
  under `primitives.integrations.<app>.<tool>`.
- `unity-deploy` contributes a package only when Unify needs local code,
  deploy-time guidance/secrets, or deterministic tests that cannot be
  delegated to a provider backend.

## Unified App Catalog

The Builtins app catalog contains two source types:

- `source_type="native"` / `Native`: Unity-deploy package manifests projected
  as app-only catalog records. These rows make native packages searchable beside
  provider apps, but they do not create provider tool catalog rows.
- `source_type="third_party"` / `Third-party`: Composio, Pipedream, or custom
  provider-backed apps whose connection state, scopes, policies, and execution
  live in Orchestra.

The actor should use `primitives.integrations.search_integrations` for app
support/connectivity/activation questions, then use normal FunctionManager
search to find executable functions or tools. Native execution remains
deployment-enabled; third-party execution remains connection-enabled.

Backend enablement is durable Orchestra configuration:
`IntegrationBackend.status` decides whether a backend such as `composio` or
`pipedream` participates in search and connection flows. `config_json` is only
for operational knobs such as `timeout_seconds`, `max_pages`, `max_items`, or
`allowed_origins`; provider credentials, project IDs, and base URLs come from
deployment environment variables.

## Staging/Production Enablement

Set provider env vars on the Orchestra deployment first. Do not send API keys,
secret env-var names, project IDs, or provider base URLs through backend setup
payloads.

Required examples:

```bash
COMPOSIO_API_KEY=...

PIPEDREAM_CLIENT_ID=...
PIPEDREAM_CLIENT_SECRET=...
PIPEDREAM_PROJECT_ID=...
# Optional, when the deployment differs from provider defaults:
PIPEDREAM_ENVIRONMENT=production
PIPEDREAM_ACCESS_TOKEN=...
```

Hosted staging and production use the Builtins artifacts job path owned by Cloud Build:

- Staging job: `unity-seed-builtins-staging`
- Production job: `unity-seed-builtins`
- `deploy/scripts/run_seed_builtins_artifacts_job.sh` creates/updates the job
  and starts it with `--async`.
- The Cloud Run Job service account must already have Secret Manager accessor
  permission for `ORCHESTRA_ADMIN_KEY` and `GLOBAL_UNIFY_KEY`. Cloud Build does
  not grant or mutate secret IAM during deploy.
- The job runs `scripts/seed_builtins_catalog.py` with the hosted integration
  manifest and `UNITY_INTEGRATION_BOOTSTRAP_EXECUTOR=api`.
- Integration artifact materialization writes `IntegrationBootstrapState` as `running`,
  `success`, or `failed`.
- The main Cloud Build starts artifact seeding and does not wait for artifact
  completion.
- `deploy/scripts/wait_builtins_artifacts.sh` is the separate validation gate
  that polls bootstrap state for the integration artifact desired hash.

Self-host deployments use the direct worker executor:

```bash
poetry run python scripts/run_builtins_artifacts_seed_self_host.py \
  --manifest deploy/integrations/bootstrap.selfhost.toml \
  --backend-id composio \
  --workers 4 \
  --batch-size 25
```

The inline API endpoint is for explicit local/small self-host operation only.
Do not automatically reroute a failed Cloud Run Job or failed direct worker into
the API path.

## Rollout Gates

Before submitting a real Cloud Build, run the local contract check:

```bash
bash deploy/scripts/check_cloudbuild_locally.sh
```

This validates the staging and production Cloud Build seed-step wiring, then
dry-runs `run_seed_builtins_artifacts_job.sh` for both environments with local
fixture manifests. It does not call GCP.

Run staging first with the same code path production will use:

```bash
gcloud builds submit . \
  --config deploy/cloudbuild-staging.yaml
```

After the staging build starts the async job, run the wait gate with the
`desired_hash` printed by the launcher:

```bash
ORCHESTRA_ADMIN_KEY=... \
bash deploy/scripts/wait_builtins_artifacts.sh \
  --orchestra-url https://internal.example.com/v0 \
  --admin-key-env ORCHESTRA_ADMIN_KEY \
  --environment staging \
  --artifact-kind integrations \
  --backend-id composio \
  --desired-hash <desired_hash>
```

Then rerun the same request JSON through
`deploy/scripts/run_seed_builtins_artifacts_job.sh --request-file` and confirm:

- Cloud Build completes under the deployment timeout.
- The Cloud Run Job exits successfully with final JSON status.
- `IntegrationBootstrapState.last_status == "success"`.
- `Builtins/Integrations/Meta` contains job, unit hash, and batch checkpoint rows.
- A same-hash rerun reports skipped tool batches before provider fetch.
- Console gallery/search/connect flows still work.

Only after staging passes the rerun gate, run production:

```bash
gcloud builds submit . \
  --config deploy/cloudbuild.yaml
```

Rollback and retry options:

- Re-run the same request JSON with the same manifest hash to resume from
  checkpoints.
- Disable the Cloud Build integration sync step only as an intentional rollback
  action. Do not route hosted failures to the inline API path.
- Console and SDK catalog readers should use Builtins logging contexts through
  the logging API; legacy Orchestra catalog projection routes are removed.

Then enable or disable backend rows with status-only PATCH calls:

```http
PATCH /v0/admin/integrations/backends/composio
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"status":"enabled"}

PATCH /v0/admin/integrations/backends/pipedream
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"status":"enabled"}
```

Use explicit sync calls to choose catalog size. A partial staging sync names the
apps and bounds tools/components per app:

```http
POST /v0/admin/integrations/sync
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"backend_id":"composio","app_slugs":["GMAIL","SLACK","HUBSPOT"],"tool_limit_per_app":50,"include_all_managed_apps":false,"create_auth_configs":true}

POST /v0/admin/integrations/sync
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"backend_id":"pipedream","app_slugs":["slack","github","hubspot"],"component_limit_per_app":50,"include_all_apps":false}
```

A full sync leaves the app list empty and opts into all provider apps:

```http
POST /v0/admin/integrations/sync
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"backend_id":"composio","app_slugs":[],"tool_limit_per_app":0,"include_all_managed_apps":true,"create_auth_configs":true}

POST /v0/admin/integrations/sync
Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
Content-Type: application/json

{"backend_id":"pipedream","app_slugs":[],"component_limit_per_app":0,"include_all_apps":true}
```
