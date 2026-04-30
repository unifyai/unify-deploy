# Private Integration Framework

This package is the deploy-side source of truth for third-party integrations.
Deployments opt into integrations by slug, and `unity-deploy` expands those
slugs into the existing Unity manager sync surfaces: guidance, secrets,
FunctionManager function directories, optional virtual environments, URL
mappings, and MCP configuration metadata.

## Flow

```
DeploymentSpec / SeedLayer
    |
    | integrations: ["github", "fetch_mcp"]
    v
resolve() -> ResolvedCustomization
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
```

The direction is intentionally simple: `unity-deploy` imports stable Unity
manager types and APIs, then prepares deployment-specific seed data around
them. The core Unity managers remain the runtime authority for execution and
storage.

## Directory Layout

Integration packages are intentionally separated by ownership and runtime
use. Discovery, activation, and tests follow the same separation:

```
unity_deploy/customization/integrations/
+-- __init__.py
+-- types.py                  # manifest schema and tier metadata
+-- discovery.py              # multi-root package and entry-point discovery
+-- loader.py                 # manifest -> Guidance/Secret/function dirs/MCP configs
+-- validation.py             # structural validation for packages
+-- activation.py             # expands enabled slugs into ResolvedCustomization
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
from unity.function_manager.custom import custom_function


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
tests/parallel_run.sh tests/customization/integrations
```

The deploy runner is the default because it prepares the right project mode,
environment, tmux isolation, logs, and per-session settings for this repo.
Sync tests use deploy's lightweight conftest plus explicit per-test contexts;
they do not rely on Unity's heavier global test lifecycle.

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
* Scenario runtime tests (`tests/customization/scenarios/test_scenarios.py`)
  cover side-effecting tick logic with a fake DataManager. Anything that
  needs a real DataManager ingest is reserved for the staging smoke order
  documented below.

Useful narrower commands:

```bash
tests/parallel_run.sh tests/customization/integrations
tests/parallel_run.sh tests/customization/integrations/sync
tests/parallel_run.sh --timeout 300 tests/customization/integrations/sync/test_function_execution.py
tests/parallel_run.sh tests/customization/scenarios
.venv/bin/python tests/customization/integrations/validate_e2e.py
.venv/bin/python tests/customization/integrations/validate_e2e.py --real
```

To add live callable execution coverage for a new API-tier integration in any
root, add an entry to `EXECUTION_CONFIG` in
`tests/customization/integrations/sync/test_function_execution.py`. If the
functions import third-party packages, also provide a matching test venv entry.

### Staging Smoke Order

Local tests cannot exercise functions that hit a real `DataManager` ingest or
the offline task activation lane. Run these in order against staging after a
deploy/release that includes the affected integration:

1. **FunctionManager registration smoke** -- invoke
   ``unity_deploy.customization.scenarios.cli`` with ``--no-materialize
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
