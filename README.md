# unity-deploy

Private enterprise deployment overlay for [Unity](https://github.com/unifyai/unity). Contains client-specific assistant deployments, seed data, ingestion pipelines, and the CI/CD infrastructure that produces production Docker images.

Unity is the open-source AI assistant framework. This repository adds the enterprise layer on top -- client configurations, memoized functions, business rules, and deployment automation -- without modifying Unity's core.

## Architecture

```
unity (public)          unity-deploy (private)
┌──────────────────┐    ┌──────────────────────────┐
│  _init_managers   │    │  unity_deploy/            │
│    ↓              │    │    hook.py ← startup_hook │
│  entry_points()  ─┼──→ │    assistant_deployments/         │
│    ↓              │    │      clients/             │
│  Actor(**kwargs)  │    │        client_alpha/     │
│                   │    │        clientgamma/           │
└──────────────────┘    │      seed_sync.py         │
                        │      environments/        │
                        │  deploy/                  │
                        │    Dockerfile             │
                        │    cloudbuild-*.yaml      │
                        └──────────────────────────┘
```

Unity discovers this package at runtime via Python [entry points](https://packaging.python.org/en/latest/specifications/entry-points/). When the `_UNITY_STARTUP_HOOK_GROUP` environment variable is set (via K8s Secrets in enterprise deployments), Unity calls `importlib.metadata.entry_points()` to find and execute the startup hook declared in this package's `pyproject.toml`. In open-source deployments where the env var is absent, the mechanism is completely inert.

The startup hook performs three tasks during manager initialization:

1. **Resolve assistant deployment** -- deployment-matched spec with optional shared seed layers (org/team/user/assistant) merged in scope order, plus secrets from `.secrets.json`.
2. **Sync seed data** -- hash-based idempotent sync of contacts, guidance, knowledge, secrets, and blacklist entries to the Unify backend.
3. **Sync custom functions** -- upsert client-specific memoized Python functions and virtual environments via `FunctionManager.sync_custom()`.

## Repository Structure

```
unity-deploy/
├── pyproject.toml                    # Package metadata + entry point declaration
├── .pre-commit-config.yaml           # Hooks matching Unity's config
├── base/
│   ├── Dockerfile                    # Mirrored Unity base-image deploy assets
│   ├── cloudbuild-staging.yaml       # Mirrored base-image Cloud Build config
│   ├── cloudbuild.yaml               # Mirrored base-image Cloud Build config
│   ├── entrypoint.sh                 # Mirrored runtime entrypoint
│   ├── desktop/                      # Mirrored desktop stack for hosted sessions
│   └── scripts/                      # Mirrored hosted job-watcher/log-upload assets
├── deploy/
│   ├── Dockerfile                    # Thin enterprise overlay image
│   ├── cloudbuild-staging.yaml       # Overlay build trigger for staging
│   ├── cloudbuild.yaml               # Overlay build trigger for production
│   └── cloudbuild-preview.yaml       # Overlay build trigger for preview
└── unity_deploy/
    ├── hook.py                       # Entry point: startup_hook()
    └── assistant_deployments/
        ├── clients/
        │   ├── __init__.py           # Deployment registry, resolve()
        │   ├── client_alpha/        # Client Alpha config, seed data, ingestion
        │   └── clientgamma/              # ClientGamma config, M365 auth, demo scenarios
        ├── configs/types/            # ActorConfig Pydantic model
        ├── environments/             # Serialized environment reconstruction
        ├── types/                    # PipelineConfig schema
        ├── seed_sync.py              # Generic hash-based seed data sync
        └── secrets_file.py           # .secrets.json parser
```

## Migration Note

During the hosted deploy split, `base/` is the private mirror of the hosted base-image
assets that still live in `unity/deploy/` today. Treat `base/` as the canonical private
copy while the live production/staging triggers continue to run from `unity`; cutover
should happen only after the private path is verified end-to-end.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:unifyai/unity-deploy.git
cd unity-deploy
uv sync --all-groups
pre-commit install
```

## Adding a New Client

1. Create a new directory under `unity_deploy/assistant_deployments/clients/<client_name>/`.
2. Define deployment packages under `deployments/<name>/` (each exposes a `DeploymentSpec`, typically via `get_deployment()`), and an `__init__.py` that builds an `EnvironmentConfig` / `DeploymentMapping` and calls `register_client()` from `unity_deploy.assistant_deployments.deployment_types`.
3. Add the client import to the bottom of `unity_deploy/assistant_deployments/clients/__init__.py` so the client self-registers at module load time.
4. If the client has seed secrets with runtime values, add entries to `.secrets.json` (gitignored, never committed).

## Deployment

Cloud Build triggers fire on branch pushes:

| Branch    | Trigger                    | Image name        | Environment |
| --------- | -------------------------- | ----------------- | ----------- |
| `staging` | `unity-deploy-staging`     | `unity-staging`   | Staging     |
| `main`    | `unity-deploy-production`  | `unity`           | Production  |
| `preview` | `unity-deploy-preview`     | `unity-preview`   | Preview     |

Each build clones Unity (matching branch), installs this package on top, pushes the image to Artifact Registry, updates the GCS image hash, and refreshes the GKE idle job pool. The communication adapters consume these images without any awareness of the overlay -- the image names and hash mechanism are unchanged.

## Branch Convention

Mirrors Unity:

- **`staging`** -- development and testing
- **`main`** -- production
- **`preview`** -- isolated preview environment
