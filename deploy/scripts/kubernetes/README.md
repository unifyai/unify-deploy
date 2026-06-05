# Kubernetes Setup Scripts

This directory contains scripts that interact directly with Kubernetes for
**one-time cluster setup**: creating ConfigMaps, Secrets, PriorityClasses, the
shared service account, and the keep-alive pod.

**WARNING:** These scripts are intended **ONLY for one-time setup** of the
cluster. They are *not* used for production orchestration, and they are *not*
part of the core assistant lifecycle.

## Where Unity Job creation actually lives (production)

There is no `create_job.py` in this directory anymore -- it was a legacy
manual-testing CLI with a stale Job manifest (missing `DEPLOY_ENV`, EVENTBUS
publish vars, the `unity-status` / `unity-date` / `unity-image-hash` labels,
the `unity-idle` PriorityClass, plus a couple of bugs). It was deleted to
avoid drift confusion.

The canonical Unity Job manifest builder is:

* [`communication/infra/helpers.py:create_unity_job`](../../../communication/infra/helpers.py)
  -- the **single source of truth** for the `batch/v1` Job manifest used by
  the production idle pool, the AssistantSession controller (override path),
  offline tasks, and dashboard actions.

It's reached via these HTTP entry points:

| Endpoint | Caller | When |
|----------|--------|------|
| `POST /infra/job/create` | Cron, scheduled refresh, operators | Create one idle Job |
| `POST /scheduled/jobs/create` (adapters) | Cloud Build (post-deploy), Cloud Scheduler hourly | Refresh the idle Job pool to target size |
| `POST /infra/job/start` | Adapters webhook handlers | Activate an assistant (creates an `AssistantSession` CR; controller binds an idle Job or spawns one if image override is set) |

## Operator escape hatches (no need for `create_job.py`)

For manual one-off Job creation during incidents or local debugging, use the
existing dev scripts (they HTTP into the production path so the manifest stays
in sync):

| Tool | Purpose |
|------|---------|
| [`scripts/dev/idle_job_refresh.py`](../dev/idle_job_refresh.py) | Refresh the idle Job pool for staging / production / preview. |
| [`scripts/dev/job_utils.py`](../dev/job_utils.py) | List / read / patch labels on existing Unity Jobs via `/infra/jobs`. |
| [`scripts/dev/suspend_job.py`](../dev/suspend_job.py) | Stop a running Job via `/infra/job/stop`. |
| [`scripts/dev/wake_and_watch.py`](../dev/wake_and_watch.py) | Spawn-and-watch flow for end-to-end tests. |

Or call the HTTP endpoints directly:

```bash
# Create one staging Job (uses the canonical create_unity_job manifest).
curl -X POST "https://service.a.run.app/infra/job/create" \
  -H "Authorization: Bearer $ORCHESTRA_ADMIN_KEY" \
  -F "namespace=staging" \
  -F "image=us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity-staging:<commit>"
```

## What's still in this directory

| Script | Purpose | When to run |
|--------|---------|-------------|
| `_k8s_lib.py` | Shared `ensure_kube_config()` helper + cluster identity constants (`PROJECT_ID`, `REGION`, `CLUSTER_NAME`). Module-private; imported by the sibling scripts. | (not run directly) |
| `setup_k8s_config.py` | Create ConfigMaps + Secrets + service account + RBAC | Once per cluster (or when rotating secrets) |
| `setup_priority_classes.py` | Create cluster-wide PriorityClasses (`unity-idle`, etc.) | Once per cluster |
| `create_keep_alive.py` | Deploy a tiny keep-alive pod that pins a GKE node warm | Once per node pool |
| `test_metrics_push.py` | Smoke-test custom-metrics push to Cloud Monitoring | Manual debugging |

The three setup scripts all defer kubeconfig setup to
`_k8s_lib.ensure_kube_config()`; each one's `setup_kubernetes_client()`
is now a 3-line wrapper that just picks the right `client.XxxApi()` for
its needs.
