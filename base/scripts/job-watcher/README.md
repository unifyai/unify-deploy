# Job Watcher

A lightweight K8s operator that watches Droid pod terminations and runs
crash-safe VM cleanup.  Built on [kopf](https://kopf.dev/) (Kubernetes
Operator Pythonic Framework).

## Why

VM release must happen regardless of how a Droid container exits
(graceful, OOMKill, segfault, node failure).  Running this logic inside
the container is unreliable in crash scenarios.

The job-watcher runs **outside** Droid containers on the same GKE cluster
and is the **sole owner** of crash-safe VM release.  The Droid
container's `mark_job_done()` handles the same release on graceful exit,
plus session duration metrics and the K8s label patch.

kopf manages the watch stream lifecycle, reconnection, error isolation,
and liveness probes — reacting within seconds of any pod termination.

## How it works

The watcher registers a kopf event handler on pods with
`label_selector=app=droid`.  kopf manages the underlying K8s watch
stream (persistent push connection, automatic reconnection,
`resourceVersion` tracking).

When a pod reaches `Succeeded` or `Failed`:
- Calls the comms service to release any pool VM assigned to that
  assistant (with retries and disk-detach fallback).

Each handler invocation is isolated — if one cleanup fails, it doesn't
affect processing of other events.  kopf handles retries and error
tracking automatically.

## Code structure

VM release and related operations (disk detach) live in a shared module:
`droid/conversation_manager/assistant_jobs_api.py`.  This file uses
`requests` for comms-service calls, with no Droid-specific dependencies.
It is used by both:

- **`assistant_jobs.py`** (Droid container) — thin wrapper that reads
  `SESSION_DETAILS`/`SETTINGS`, records Prometheus metrics, and delegates
  all HTTP operations to `assistant_jobs_api`.
- **`watcher.py`** (this operator) — thin kopf handler that reads env
  vars and delegates cleanup to `assistant_jobs_api`.

The Dockerfile copies `assistant_jobs_api.py` from the Droid source tree
at build time and clones/installs the `unify` SDK from GitHub (the deploy
scripts set the Docker build context to the repo root and pass
`GITHUB_TOKEN` for private repo access).

## Responsibility split

| Component | When it runs | What it does |
|---|---|---|
| `mark_job_done()` (in Droid container) | Graceful exit | K8s label patch + VM release + session duration metric |
| **job-watcher** (this) | Any exit (crash-safe) | VM release |
| `expire_all_stale_jobs()` (adapters) | Periodic sweep | Safety net: suspend stale K8s jobs + VM release |

All layers call the same idempotent operations.  Running any
combination is harmless.

## Deployment

The job-watcher is built and deployed automatically by Cloud Build
alongside the main Droid image (`cloudbuild.yaml` / `cloudbuild-staging.yaml`).
Every push to `staging` or `main` rebuilds the watcher image in parallel
with the Droid image and applies the deployment manifest via `kubectl apply`
(creates the Deployment on first run, updates the image on subsequent runs).

The brief restart (~5 seconds) is safe: kopf replays recent events on
startup, and all cleanup operations are idempotent.

### Verify

```bash
# Check the pod is running
kubectl get pods -n staging -l app=job-watcher

# Tail logs
kubectl logs -n staging -l app=job-watcher -f

# Check health
kubectl exec -n staging deploy/job-watcher -- curl -s localhost:8080/healthz
```

## Resource footprint

| Resource | Request | Limit |
|---|---|---|
| CPU | 50m | 100m |
| Memory | 64Mi | 128Mi |

## Environment variables

Injected via `droid-config` (ConfigMap) and `droid-secrets` (Secret):

| Variable | Source | Purpose |
|---|---|---|
| `ORCHESTRA_URL` | deployment manifest | Orchestra API base URL, used by `unify` SDK (differs per environment) |
| `DROID_COMMS_URL` | deployment manifest | Comms service base URL (differs per environment) |
| `ORCHESTRA_ADMIN_KEY` | droid-secrets | Auth for comms infra endpoints |
