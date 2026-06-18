# Private Integration Framework

This package is the deploy-side source of truth for third-party integrations.
Deployments opt into integrations by slug, and `unity-deploy` expands those
slugs into the existing Droid manager sync surfaces: guidance, secrets,
FunctionManager function directories, optional virtual environments, URL
mappings, and MCP configuration metadata.

Provider-backed apps from Composio, Pipedream, or another hosted integration
backend do not need a package here for every supported SaaS app. See
[`PROVIDER_BACKED_INTEGRATIONS.md`](PROVIDER_BACKED_INTEGRATIONS.md) for the
boundary: Builtins owns provider app/tool catalog artifacts, Orchestra owns
connection state and tool execution; Droid surfaces those tools as
`primitives.integrations.<app>.<tool>` virtual FunctionManager records;
`unity-deploy` remains the Level 3 full-package path for custom runtime code.

Native package manifests are also projected into Builtins `Integrations/Apps`
as `source_type="native"` app records. Those rows exist for actor discovery
only: they let `primitives.integrations.search_integrations` return `Native`
and `Third-party` apps in one result set. Native package functions are not
copied into provider tool rows; they continue to materialize through the
existing FunctionManager package sync path.

## Flow

```
DeploymentSpec / SeedLayer
    |
    | integrations: ["github", "fetch_mcp"]
    v
resolve() -> ResolvedAssistantDeployment
    |
    v
expand_integrations()
    |
    +-- guidance -> sync_all_seed_data() -> GuidanceManager
    +-- secrets -> sync_all_seed_data() -> SecretManager
    +-- function_dirs -> FunctionManager sync path
    +-- venv_dirs -> FunctionManager sync path
    +-- url_mappings -> deployment hook runtime mappings
    +-- mcp_configs -> loaded for future MCP runtime registration
    +-- integration_registry -> Integrations/Manifests telemetry
    +-- native catalog projection -> Builtins Integrations/Apps (`Native`)
```

The direction is intentionally simple: `unity-deploy` imports stable Droid
manager types and APIs, then prepares deployment-specific seed data around
them. The core Droid managers remain the runtime authority for execution and
storage.

## Directory Layout

Integration packages are intentionally separated by ownership and runtime
use. Discovery, activation, and tests follow the same separation:

```
unity_deploy/assistant_deployments/integrations/
+-- __init__.py
+-- types.py                  # manifest schema and tier metadata
+-- discovery.py              # multi-root package and entry-point discovery
+-- loader.py                 # manifest -> Guidance/Secret/function dirs/MCP configs
+-- validation.py             # structural validation for packages
+-- activation.py             # expands enabled slugs into ResolvedAssistantDeployment
+-- mcp_adapter.py            # MCP tool discovery and wrapper-source generation
+-- aggregate_demo_sites.py   # browser-tier demo-site copy helper
+-- packages/                 # generic root: reusable platform/provider connectors
|   +-- github/               # API-tier example
|   +-- fetch_mcp/            # MCP-tier example
+-- client_packages/          # client root: private real connectors / compositions
|   +-- client_alpha_repairs/
+-- mock_packages/            # mock root: opt-in deterministic test doubles
    +-- client_alpha_repairs_mock/
```

| Root | Owner / Audience | Activation |
|------|------------------|------------|
| `packages/` | Platform; reusable across clients | Default |
| `client_packages/` | One named client; private composition or real connector | Default |
| `mock_packages/` | Internal scenario E2Es and offline pilot smoke | Opt-in only |

Each integration package must include a `manifest.yaml`. Optional directories
are loaded by convention:

| Path | Purpose |
|------|---------|
| `functions/` | Python functions registered into FunctionManager |
| `guidance/` | Markdown guidance synced into GuidanceManager |
| `venvs/` | Virtual environment definitions for third-party Python dependencies |
| `demo_site/` | Browser-tier demo site assets copied by `aggregate_demo_sites.py` |
| `scenarios/` | YAML scenario specs loaded by the scenario runtime |

## Manifest Shape

The manifest is the entry point for discovery and activation. At minimum it
declares identity and tier:

```yaml
name: GitHub
slug: github
tier: api
quality: silver
description: GitHub REST API integration for repository, user, and issue lookup.
```

API-tier packages can declare `requirements`, secrets, capabilities, functions,
and guidance references. MCP-tier packages declare an `mcp` block that describes
how to launch or connect to the MCP server.

## Enabling Integrations

Deployment specs can enable integrations directly:

```python
DeploymentSpec(
    name="v1",
    actor_config=ActorConfig(...),
    integrations=["github", "fetch_mcp"],
)
```

Seed layers can also contribute integrations:

```python
register_layer(
    "client_name",
    "assistant",
    "123",
    SeedLayer(integrations=["github"]),
)
```

Layer-level integrations are merged after deployment-level integrations. Order
is preserved and duplicate slugs are removed.

`DeploymentSpec.integrations` and `SeedLayer(integrations=[...])` accept only
disk package slugs discovered under `packages/`, `client_packages/`, opt-in
`mock_packages/`, entry points, or explicit search paths. Do not add
`composio`, `pipedream`, `hubspot`, `salesforce`, or other provider-backed app
names here unless a real Level 3 package with that slug exists in this repo.
Dynamic provider-backed connection state lives in Orchestra, not in
`Integrations/Manifests`.

## Provider Backend Operations

For staging and production, provider credentials are deployment env vars on
Orchestra. Backend API calls should only toggle status or operational knobs; do
not send provider API keys, provider base URLs, Pipedream project IDs, or env-var
names in backend payloads.

Typical staging enablement is:

```http
PATCH /v0/admin/integrations/backends/composio
{"status":"enabled"}

PATCH /v0/admin/integrations/backends/pipedream
{"status":"enabled"}
```

Partial sync is explicit and bounded:

```http
POST /v0/admin/integrations/sync
{"backend_id":"composio","app_slugs":["GMAIL","SLACK","HUBSPOT"],"tool_limit_per_app":50,"include_all_managed_apps":false,"create_auth_configs":true}

POST /v0/admin/integrations/sync
{"backend_id":"pipedream","app_slugs":["slack","github","hubspot"],"component_limit_per_app":50,"include_all_apps":false}
```

Full sync opts into all apps with empty `app_slugs` plus `include_all_*: true`.
See [`PROVIDER_BACKED_INTEGRATIONS.md`](PROVIDER_BACKED_INTEGRATIONS.md) for the
full environment variable and Postman-ready request details.

## Native vs Third-party App Discovery

The actor sees a unified app discovery surface but the runtime ownership stays
source-specific:

| Source label | Source of support | Activation signal | Execution discovery |
|--------------|-------------------|-------------------|---------------------|
| `Native` | `manifest.yaml` in this repo, projected to Builtins `Integrations/Apps` as an app-only catalog row | Deployment enablement plus required secrets in `Integrations/Manifests` / SecretManager | FunctionManager search over synced package functions |
| `Third-party` | Builtins `Integrations/Apps` and `Integrations/Tools` log rows seeded from provider artifacts | Provider connection state and backend policy in Orchestra | FunctionManager search over materialized provider tool rows |

This separation avoids duplicating native functions as provider tools while
still letting the actor answer "is this app supported, active, connected, or
still syncing?" before it searches for executable functions.

## Connector Tiers

| Tier | Assets | Runtime Behavior |
|------|--------|------------------|
| `api` | `functions/`, `guidance/`, optional `venvs/` | Functions are synced into FunctionManager and can be invoked by the Actor |
| `mcp` | `manifest.yaml`, `guidance/`, `mcp` block | MCP configs are loaded; runtime wrapper registration is deferred |
| `browser` | `demo_site/`, `guidance/`, optional functions | Demo sites can be copied into agent-service with `aggregate_demo_sites.py` |

## Built-In Integrations

| Slug | Root | Tier | Purpose |
|------|------|------|---------|
| `github` | `packages/` | API | GitHub REST API example with mock-safe functions for users, repos, and issues |
| `fetch_mcp` | `packages/` | MCP | Official MCP Fetch server example (`@modelcontextprotocol/server-fetch`) |
| `clientepsilon_homes_compliance` | `client_packages/` | API | Private ClientEpsilon Homes compliance connector for SharePoint-backed certificate assurance (fail-closed until Graph credentials land) |
| `clientepsilon_homes_compliance_mock` | `mock_packages/` | API | Deterministic ClientEpsilon compliance mock used for certificate renewal demo and video capture |
| `client_alpha_repairs` | `client_packages/` | API | Private Client Alpha repairs client connector (fail-closed until live credentials land) |
| `client_alpha_repairs_mock` | `mock_packages/` | API | Deterministic Client Alpha repairs mock used by scenario E2Es; activated only when `include_mock_packages=True` or a `*_mock` slug is enabled |

## FunctionManager-Compatible Functions

FunctionManager executes each registered function in an isolated namespace.
Module-level globals are not available when the Actor retrieves a callable, so
integration functions must be self-contained.

Follow these rules:

1. Put mock data and constants inside the function body.
2. Put imports inside the function body, including stdlib imports like `os`.
3. Decorate every function that should be registered with `@custom_function()`.
4. Decorate helper functions too if another registered function depends on them.
5. Return JSON-serializable values.
6. Avoid `print()` and `logger.*()` calls in function bodies.
7. If a function imports third-party packages, register it with a FunctionManager `venv_id`.

Correct pattern:

```python
from droid.function_manager.custom import custom_function


@custom_function()
async def get_user(username: str, mock: bool = True) -> dict:
    if mock:
        users = {"octocat": {"login": "octocat"}}
        return users.get(username, {"error": "not found"})

    import os
    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/users/{username}",
            headers=headers,
        )
        response.raise_for_status()
        return response.json()
```

## Testing

Run integration tests from the `unity-deploy` repo with its own runner:

```bash
tests/parallel_run.sh tests/assistant_deployments/integrations
```

The deploy runner is the default because it prepares the right project mode,
environment, tmux isolation, logs, and per-session settings for this repo.
Sync tests use deploy's lightweight conftest plus explicit per-test contexts;
they do not rely on Droid's heavier global test lifecycle.

Coverage is split intentionally so that mock packages get the same
FunctionManager guarantees as production connectors:

* Symbolic AST tests (`test_function_compliance.py`, `test_builtin_integrations.py`)
  cover all three roots automatically -- generic, client, and mock. Mocks are
  always included because the rules are structural and apply to anything
  destined for FunctionManager.
* Live registration and execution tests (`sync/test_function_execution.py`)
  also cover all three roots. ``EXECUTION_CONFIG`` entries pin a
  representative callable per integration -- including
  ``client_alpha_repairs_mock`` -- so we exercise the same registration
  surface that scenario E2Es and offline task activations rely on.
* Scenario runtime tests (`tests/assistant_deployments/scenarios/test_scenarios.py`)
  cover side-effecting tick logic with a fake DataManager. Anything that
  needs a real DataManager ingest is reserved for the staging smoke order
  documented below.

Useful narrower commands:

```bash
tests/parallel_run.sh tests/assistant_deployments/integrations
tests/parallel_run.sh tests/assistant_deployments/integrations/sync
tests/parallel_run.sh --timeout 300 tests/assistant_deployments/integrations/sync/test_function_execution.py
tests/parallel_run.sh tests/assistant_deployments/scenarios
.venv/bin/python tests/assistant_deployments/integrations/validate_e2e.py
.venv/bin/python tests/assistant_deployments/integrations/validate_e2e.py --real
.venv/bin/python deploy/scripts/dev/run_clientepsilon_compliance_scenario.py --no-materialize --no-outbox
.venv/bin/python deploy/scripts/dev/run_clientepsilon_compliance_scenario.py --tick 0
```

To add live callable execution coverage for a new API-tier integration in any
root, add an entry to `EXECUTION_CONFIG` in
`tests/assistant_deployments/integrations/sync/test_function_execution.py`. If the
functions import third-party packages, also provide a matching test venv entry.

### Staging Smoke Order

Local tests cannot exercise functions that hit a real `DataManager` ingest or
the offline task activation lane. Run these in order against staging after a
deploy/release that includes the affected integration:

1. **FunctionManager registration smoke** -- invoke
   ``unity_deploy.assistant_deployments.scenarios.cli`` with ``--no-materialize
   --no-outbox`` to confirm the integration's functions register and execute
   in mock mode end-to-end.
2. **Scenario tick smoke** -- run the same CLI without the ``--no-*`` flags
   so the scenario tick materializes contexts through the real
   ``DataManager`` and writes the simulated alert outbox.
3. **Offline task activation smoke** -- trigger the materialized
   ``ScheduledTaskActivation`` (e.g. via the reconcile job's
   ``run_now`` path) and confirm the headless lane wakes the activation,
   runs the entrypoint function, and clears the activation.

Steps 1-2 cover the FunctionManager and scenario surfaces; step 3 covers the
generic offline task activation lane that orchestrates scheduled scenarios in
production. Failures at any step should block promotion of the
integration's deployment.

## Current Caveat

`mcp_adapter.py` can discover MCP tools and generate FunctionManager-compatible
wrapper source. Runtime MCP wrapper registration is intentionally deferred in
`hook.py` for now because the generated wrappers still need a durable runtime
call path to the MCP subprocess, not just a JSON-RPC request payload.
