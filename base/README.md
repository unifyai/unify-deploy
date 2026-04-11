# Base Image Build

This directory contains the deployment files that were previously in `unity/deploy/`.
They were moved here to keep the Unity repo clean for open-source release.

## Structure

```
base/
├── Dockerfile              # Full Unity base image (Python, Node, system deps, app code)
├── entrypoint.sh           # Container entrypoint (memory watchdog, display, app startup)
├── cloudbuild.yaml         # Cloud Build config for production base image + job-watcher
├── cloudbuild-staging.yaml # Cloud Build config for staging
├── cloudbuild-preview.yaml # Cloud Build config for preview
├── desktop/                # Virtual desktop container (VNC/noVNC, audio, browser)
│   ├── Dockerfile
│   ├── display.sh          # X11 virtual display setup (invoked by entrypoint.sh)
│   ├── device.sh           # Virtual audio/video devices (invoked by entrypoint.sh)
│   └── ...
└── scripts/
    ├── upload_pod_logs.py   # GCS log upload on shutdown (called from unity main.py)
    └── job-watcher/         # Kopf operator for K8s job lifecycle
        ├── Dockerfile
        ├── watcher.py
        └── deployment*.yaml
```

## How the build pipeline works

The base Cloud Build configs (in this directory) clone the Unity source repo,
inject this `base/` directory as `deploy/` inside the checkout, then build the
Docker image. This means the Dockerfile's `COPY . /app` picks up the full Unity
source plus the deployment files.

1. **Cloud Build trigger fires** (on push to unity-deploy)
2. `clone-unity` step clones `unifyai/unity` and copies `base/` → `deploy/`
3. `build-image` step runs `docker build` from the Unity checkout
4. Base image is pushed to Artifact Registry
5. `trigger-deploy-build` step triggers the overlay build (`deploy/cloudbuild.yaml`)

## GCP Cloud Build Trigger Setup

The Cloud Build triggers that previously pointed at the `unity` repo should now
point at `unity-deploy` and use these config files:

| Environment | Config file | Trigger name |
|-------------|-------------|--------------|
| Production  | `base/cloudbuild.yaml` | `unity-base` |
| Staging     | `base/cloudbuild-staging.yaml` | `unity-base-staging` |
| Preview     | `base/cloudbuild-preview.yaml` | `unity-base-preview` |

Update the triggers in GCP Console → Cloud Build → Triggers.
