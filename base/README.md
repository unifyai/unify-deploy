# Deployment

Docker image and CI/CD configuration for the Unity container.

## Contents

- `Dockerfile` — Production container image (Python, Node, system deps, agent-service, browser automation)
- `entrypoint.sh` — Container entrypoint (memory watchdog, display setup, app startup)
- `cloudbuild.yaml` / `cloudbuild-staging.yaml` — GCP Cloud Build pipelines
- `desktop/` — Virtual desktop stack (VNC/noVNC, audio devices, browser) for computer-use sessions

## Docker

Build the image locally:

```bash
docker build -f deploy/Dockerfile \
  --secret id=branding_deploy_key,src=/path/to/branding_key \
  --secret id=iso_deploy_key,src=/path/to/iso_key \
  --secret id=orchestra_deploy_key,src=/path/to/orchestra_key \
  --secret id=console_deploy_key,src=/path/to/console_key \
  --build-arg UNIFY_KEY=your-key \
  -t unity .
```

The Dockerfile clones its dependencies from GitHub at build time (they're not
bundled in the image context). `unify`, `unisdk`, `unillm` and `magnitude` are
public and need no credential. The private repositories — `branding` and its
`packages/iso` submodule, `orchestra`, `console` — are each read with their own
repo-scoped read-only deploy key, supplied as a mounted secret.

Pass the keys as `--secret`, never as `--build-arg`: a build argument is
recorded verbatim in the metadata of every layer that follows it and is
readable by anyone who can pull the image.

## Cloud Build

The Cloud Build configs are triggered by pushes to `main` and `staging`. They build the base image, push to Artifact Registry, and trigger a downstream enterprise overlay build.

These configs use `${PROJECT_ID}` and other Cloud Build substitution variables — no credentials are hardcoded.
