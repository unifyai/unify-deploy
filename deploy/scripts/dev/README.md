# Dev Scripts

Developer utilities for managing Droid jobs, assistants, and logs against the live staging/production infrastructure.

Most scripts that talk to the comms service require two environment variables:

```bash
export DROID_COMMS_URL="https://..."
export ORCHESTRA_ADMIN_KEY="..."
```

Scripts that talk to the adapters service pick the correct URL automatically based on `--env` (override with `--adapters-url`).

---

## idle_job_refresh.py

Create fresh idle K8s jobs and clean up stale ones. Designed to run after a Droid Cloud Build completes.

- Creates two new idle jobs via the adapters `/scheduled/jobs/create` endpoint.
- Waits 30 s (configurable) for the jobs to register as idle.
- Calls the adapters `/scheduled/jobs/cleanup` endpoint.
- Lists all job names before, after creation, and after cleanup (disable with `--no-list-jobs`).

```bash
python scripts/dev/idle_job_refresh.py                       # staging (default)
python scripts/dev/idle_job_refresh.py --env production      # production
python scripts/dev/idle_job_refresh.py --no-list-jobs        # skip job listing
python scripts/dev/idle_job_refresh.py --delay 45            # custom wait
```

## suspend_job.py

Suspend (stop) a running Droid K8s job by name via the comms `/infra/job/stop` endpoint.

```bash
python scripts/dev/suspend_job.py                                        # auto-detect staging
python scripts/dev/suspend_job.py --env production                       # auto-detect production
python scripts/dev/suspend_job.py droid-2026-02-25-12-00-00              # explicit job
python scripts/dev/suspend_job.py droid-2026-02-25-12-00-00 --namespace custom-ns
```

## local_assistant.py

Create or fetch a local assistant from production Orchestra and print the `export` lines needed to run Droid on your machine.

```bash
# Create / fetch by name:
python scripts/dev/local_assistant.py --api-key YOUR_KEY --name "Dev Assistant"

# Fetch by ID:
python scripts/dev/local_assistant.py --api-key YOUR_KEY --id 42

# Source into your shell:
source <(python scripts/dev/local_assistant.py --api-key YOUR_KEY --name "Dev")

# Write to a .env file:
python scripts/dev/local_assistant.py --api-key YOUR_KEY --name "Dev" > .env.local
```

## keep_pod_alive.sh

Keep a deployed Droid pod alive by sending periodic keepalive pings to its Pub/Sub topic. Prevents the inactivity timeout from shutting down the container while you're debugging or developing.

```bash
./scripts/dev/keep_pod_alive.sh 25                        # staging (default), ping every 30s
./scripts/dev/keep_pod_alive.sh 25 --env production       # production
./scripts/dev/keep_pod_alive.sh 25 --interval 60          # custom interval
```

Requires `gcloud` CLI authenticated with access to the `gcp-project-runtime` project.

## run_pipeline.sh

Dispatch a pipeline job and stream all worker logs + scaling metrics to persistent log files.

Two modes:

- **Dispatch mode** (default): dispatches the job via `dispatch_pipeline.py`, then attaches log streams and monitoring.
- **Monitor mode** (`--monitor`): skips dispatch, only streams logs and metrics for an already-running pipeline.

All arguments except `--monitor` are forwarded directly to `dispatch_pipeline.py`.

```bash
# Dispatch a DM job from a config file (first 5 files only)
deploy/scripts/dev/run_pipeline.sh \
  --mode dm \
  --config path/to/pipeline_config.json \
  --project-root ~/unity-deploy \
  --user-id $USER_ID --assistant-id $ASSISTANT_ID \
  --limit 5

# Monitor an already-running pipeline (no dispatch)
deploy/scripts/dev/run_pipeline.sh --monitor
```

Creates a timestamped directory under `logs/pipeline/` with:

| File | Content |
|---|---|
| `dispatch.log` | `dispatch_pipeline.py` output (dispatch mode only) |
| `parse-worker.log` | Streaming kubectl logs for parse pods |
| `ingest-worker.log` | Streaming kubectl logs for ingest pods |
| `hpa.log` | HPA snapshots every 10s |
| `pods.log` | Pod count snapshots every 10s |
| `pubsub-backlog.log` | Pub/Sub backlog depth + replica counts every 15s |
| `summary.txt` | Final run summary |

Press `Ctrl+C` to stop all streams and generate the summary.

## setup_pipeline_infra.sh

Idempotent setup for GCP pipeline infrastructure (Pub/Sub topics/subscriptions, GCS buckets, Stackdriver Custom Metrics adapter for HPA scaling).

## job_logs/

Tooling for streaming and inspecting Droid K8s job logs. See [`job_logs/README.md`](job_logs/README.md) for setup and usage.
