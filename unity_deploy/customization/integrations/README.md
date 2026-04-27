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

```
unity_deploy/customization/integrations/
+-- __init__.py
+-- types.py                  # manifest schema and tier metadata
+-- discovery.py              # built-in and entry-point package discovery
+-- loader.py                 # manifest -> Guidance/Secret/function dirs/MCP configs
+-- validation.py             # structural validation for packages
+-- activation.py             # expands enabled slugs into ResolvedCustomization
+-- mcp_adapter.py            # MCP tool discovery and wrapper-source generation
+-- aggregate_demo_sites.py   # browser-tier demo-site copy helper
+-- packages/
    +-- github/               # API-tier example
    +-- fetch_mcp/            # MCP-tier example
```

Each integration package lives under `packages/<slug>/` and must include a
`manifest.yaml`. Optional directories are loaded by convention:

| Path | Purpose |
|------|---------|
| `functions/` | Python functions registered into FunctionManager |
| `guidance/` | Markdown guidance synced into GuidanceManager |
| `venvs/` | Virtual environment definitions for third-party Python dependencies |
| `demo_site/` | Browser-tier demo site assets copied by `aggregate_demo_sites.py` |

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

| Slug | Tier | Purpose |
|------|------|---------|
| `github` | API | GitHub REST API example with mock-safe functions for users, repos, and issues |
| `fetch_mcp` | MCP | Official MCP Fetch server example (`@modelcontextprotocol/server-fetch`) |

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

Useful narrower commands:

```bash
tests/parallel_run.sh tests/customization/integrations/sync
tests/parallel_run.sh --timeout 300 tests/customization/integrations/sync/test_function_execution.py
.venv/bin/python tests/customization/integrations/validate_e2e.py
.venv/bin/python tests/customization/integrations/validate_e2e.py --real
```

The symbolic tests validate manifest types, discovery, loading, validation, MCP
wrapper generation, demo-site aggregation, package structure, and AST
compliance. The sync tests exercise real `FunctionManager`, `GuidanceManager`,
and `SecretManager` behavior where manager contracts matter.

To add live callable execution coverage for a new API-tier integration, add an
entry to `EXECUTION_CONFIG` in
`tests/customization/integrations/sync/test_function_execution.py`. If the
functions import third-party packages, also provide a matching test venv entry.

## Current Caveat

`mcp_adapter.py` can discover MCP tools and generate FunctionManager-compatible
wrapper source. Runtime MCP wrapper registration is intentionally deferred in
`hook.py` for now because the generated wrappers still need a durable runtime
call path to the MCP subprocess, not just a JSON-RPC request payload.
