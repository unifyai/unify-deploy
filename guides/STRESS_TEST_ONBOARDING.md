# Stress Test Onboarding Guide

Comprehensive reference for the production traffic stress test infrastructure.
Written to get a new contributor (human or LLM) up to speed on the full system,
the changes made, the bugs found and fixed, and how to debug issues.

---

## 1. Architecture Overview

Unify is a platform where users hire AI assistants. The `communication` repo
handles inbound/outbound communication (SMS, email, Teams, console messages,
video calls) and the infrastructure that manages assistant containers on GKE
and desktop VMs on GCE.

### Key Components

| Component | Where | What it does |
|---|---|---|
| **Adapters** | Cloud Run (`unity-adapters-preview`) | Stateless webhook handlers. Receives inbound traffic, publishes to Pub/Sub, calls Comms App for container/VM lifecycle. |
| **Comms App** | Cloud Run (`unity-comms-app-preview`) | Container lifecycle management. `/infra/job/start` (Lease + CAS), `/infra/vm/pool/assign`, pool replenishment. |
| **Unity Containers** | GKE Jobs (namespace `preview`) | The actual AI assistants. Poll for assignment, process messages, run LLM. |
| **Pool VMs** | GCE (`gcp-project-vms` project, zone `us-central1-b` for preview) | Desktop VMs with agent-service, Caddy TLS, persistent disk. |
| **Orchestra** | External API (`internal.example.com`) | User/assistant management, credentials, billing. |

### Container Lifecycle (Lease-Based Assignment)

```
Adapter receives message
  → POST /infra/job/start on Comms App
  → Comms App acquires K8s Lease (atomic distributed lock)
  → claim_idle_container: list idle Jobs, CAS patch labels to running + write startup config as annotation
  → Release Lease
  → If pool exhausted: publish to unity-pending-startups Pub/Sub queue, return 202
  → Container polls GET /infra/job/{name}, detects unity-status=running, reads config, starts serving
```

### VM Lifecycle

```
/infra/job/start fires background task: assign_pool_vm
  → release_pool_vm (idempotent cleanup of previous assignment)
  → claim_idle_vm (GCE label CAS: pool-role=idle → assigned)
  → create/attach persistent disk
  → write metadata (unify-key, ssh-public-key, etc.)
  → VM pool watcher detects metadata change → do_assign → start agent-service
  → POST /infra/vm/ready → Comms App publishes assistant_desktop_ready to Pub/Sub
```

---

## 2. The Preview Branch

### Why a separate branch?

The `preview` branch in both `communication` and `unity` repos tests the
**K8s Lease-based container assignment** — a major architectural change that
replaces the old Pub/Sub-based startup flow. It runs on isolated infrastructure:

| Resource | Preview | Staging |
|---|---|---|
| K8s namespace | `preview` | `staging` |
| Cloud Run services | `unity-comms-app-preview`, `unity-adapters-preview` | `unity-comms-app-staging`, `unity-adapters-staging` |
| GCE VM zone | `us-central1-b` | `us-central1-a` |
| Pub/Sub topic suffix | `-staging` (Orchestra doesn't know about preview) | `-staging` |
| Container image | `unity-preview:{sha}` | `unity-staging:{sha}` |
| Cloud Scheduler | `*-preview` jobs | `*-staging` jobs |

### Key changes on preview (communication repo)

1. **K8s Lease + CAS container assignment** (`communication/infra/helpers.py`):
   - `acquire_assignment_lease` / `release_assignment_lease` — atomic distributed lock via K8s Leases
   - `claim_idle_container` — CAS label patch with resourceVersion
   - `publish_pending_startup` / `process_pending_startups` — overflow queue for pool exhaustion

2. **Centralized Settings** (`common/settings.py`):
   - Replaces scattered `DEPLOY_ENV`, `COMMS_URL`, etc. with `SETTINGS` singleton
   - Supports `DEPLOY_ENV=preview` (not just staging/production)
   - `SETTINGS.env_suffix`, `SETTINGS.assistant_topic(id)`, `SETTINGS.unity_image_name`

3. **Pending-startup Pub/Sub queue**:
   - When pool is exhausted, `/infra/job/start` returns 202 and publishes to `unity-pending-startups-preview`
   - `/infra/pending/process` reconciler pulls messages and assigns them to idle containers
   - Cloud Scheduler fires every minute as backstop

4. **Removed `is_job_running()`** from adapters:
   - Adapter now unconditionally calls `/infra/job/start` (dedup is atomic server-side)

### Key changes on preview (unity repo)

1. **Annotation polling** (`comms_manager.py`):
   - Container polls `GET /infra/job/{name}` every 500ms instead of subscribing to Pub/Sub
   - Detects `unity-status=running`, reads startup config from annotation

2. **Boot-time label race fix** (`comms_manager.py:start()`):
   - On boot, checks if already claimed before patching labels to `idle`
   - Prevents the race where the reconciler claims a container but the container's boot sequence resets it to idle

---

## 3. The Stress Test

### File: `tests/infra/integration/test_stress.py`

Single parametric test function with 8 phases, driven by `TEST_CREATE_ASSISTANT_COUNT`.

### How to run

```bash
# Quick (~5 min)
TEST_CREATE_ASSISTANT_COUNT=5 pytest tests/infra/integration/test_stress.py -v -s

# Full stress (~12 min)
TEST_CREATE_ASSISTANT_COUNT=15 pytest tests/infra/integration/test_stress.py -v -s
```

### Environment (.env)

```
UNIFY_KEY=...
SHARED_UNIFY_KEY=...
ORCHESTRA_ADMIN_KEY=...
TEST_ORCHESTRA_URL=https://internal.example.com/v0
TEST_COMMS_APP_URL=https://service.a.run.app
TEST_ADAPTERS_URL=https://service.a.run.app
TEST_NAMESPACE=preview
TEST_VM_ZONE=us-central1-b
```

### Phase Summary

| Phase | What it tests | Key assertions |
|---|---|---|
| **Setup** | Clean slate: delete all jobs, release VMs, create 3 fresh idle containers | — |
| **P1 Thundering Herd** | N concurrent `/infra/job/start` requests against 3 idle containers. Pool refresh fires concurrently. | No errors (200 or 202). No INV-1 (duplicates). |
| **P2 Traffic Firehose** | 4N adapter requests (messages, meets, events) while containers still booting | Adapter 5xx is warning, not failure |
| **P3 Steady State** | Wait for all containers running. Check VMs (single pass after 60s). Check message delivery via outbound Pub/Sub. | All N containers running. Zero VM auth failures (INV-11). |
| **P4 Sustained Load** | 3 rounds of randomized traffic with cleanup/stale-expire between rounds. INV-1 provocation (3 concurrent start_job for same assistant). | No INV-1/2/3 violations. Provocation produces exactly 1 container. |
| **P5 Crash Recovery** | Kill 2 pods, fire stale-expire during watcher processing, re-start, verify recovery. Background traffic to other assistants. | Crashed assistants recover. No INV-1. |
| **P6 Cleanup vs Startup** | Race `/scheduled/jobs/cleanup` against `start_job` (3 rounds). | No INV-8 (cleanup never kills live container). |
| **P7 Rapid Restart** | Delete job + immediate re-start. Verify VM re-attachment and auth. | New container assigned. VM auth OK. |
| **P8 Wind-down** | Delete all jobs, release all VMs, check for orphans. | No orphaned VMs (INV-10). Final invariants clean. |

### Background Scheduler Noise

A `_SchedulerNoise` thread fires cleanup, pool-refresh, and stale-expire at random 20-45s intervals throughout ALL phases, simulating production cron interference.

### Curated Scheduler Interference

In addition to the random noise:
- **P1**: `jobs/create?refresh=true` concurrent with thundering herd
- **P4**: `jobs/cleanup` between rounds 1-2, `jobs/expire-stale` between rounds 2-3
- **P5**: `jobs/expire-stale` during crash recovery (races with job-watcher)

---

## 4. Invariants Reference

The full invariant document is at `investigations/infra-invariants.md`. Key ones tested:

| ID | Invariant | How tested |
|---|---|---|
| INV-1 | At most one container per assistant | Thundering herd + INV-1 provocation |
| INV-5 | Idle pool has capacity | Pool exhaustion in P1 (expected violation, not asserted) |
| INV-8 | Cleanup never deletes live container | P6 cleanup vs startup race |
| INV-9 | Desktop containers have assigned VMs | P3 VM check |
| INV-10 | Assigned VMs have live containers | P8 orphan check |
| INV-11 | VM auth key matches | P3 + P7 auth probes |
| INV-16 | Pending startups eventually processed | P1 overflow → P3 all containers running |

---

## 5. Known Issues and Naming Conventions

### Pub/Sub naming mismatch

Orchestra creates topics/subscriptions with `-staging` suffix for all non-production
environments. The K8s namespace is `preview` and VMs use `-preview` suffix. This means:

- Topic for assistant 917: `unity-917-staging` (created by Orchestra)
- K8s Job name: `unity-...-preview` (created by Comms App using `SETTINGS.env_suffix`)
- VM hostname: `unity-pool-ubuntu-23-preview.vm.unify.ai`

The test uses `_PUBSUB_SUFFIX = "-staging"` for Pub/Sub operations and `NAMESPACE = "preview"` for K8s operations.

### VM pool capacity

The preview VM pool has ~25 ubuntu VMs. With 15 assistants, only 5 may get VMs if the pool was partially claimed. The test asserts zero auth failures for assigned VMs but doesn't assert all assistants get VMs.

### Message delivery check

Requires the outbound Pub/Sub subscription (`unity-{id}-staging-outbound-sub`) to exist.
Created by Orchestra's `create_infra=True` flow. If the subscription doesn't exist, the
check is skipped with a warning.

---

## 6. Debugging Guide

### Checking Cloud Run logs

```bash
# Comms App logs (application-level, stderr)
gcloud logging read \
  'resource.type="cloud_run_revision" resource.labels.service_name="unity-comms-app-preview" logName:"stderr"' \
  --project=gcp-project-runtime --limit=20 --format='value(timestamp,textPayload)'

# Filter by assistant ID
gcloud logging read \
  'resource.type="cloud_run_revision" resource.labels.service_name="unity-comms-app-preview" logName:"stderr" textPayload:"862"' \
  --project=gcp-project-runtime --limit=20 --format='value(timestamp,textPayload)'

# Filter by specific container
gcloud logging read \
  'resource.type="cloud_run_revision" resource.labels.service_name="unity-comms-app-preview" logName:"stderr" textPayload:"uf9c3"' \
  --project=gcp-project-runtime --limit=20 --format='value(timestamp,textPayload)'

# Key log patterns to search for:
#   "Acquired assignment lease"     — Lease acquired for container claim
#   "Claimed container"             — CAS patch succeeded
#   "CAS conflict"                  — Another caller claimed the container first
#   "Published pending startup"     — Message queued (pool exhausted)
#   "Pulled N pending startup"      — Reconciler processing messages
#   "No idle container"             — Reconciler nacked (will retry)
#   "Reconciler assigned container" — Overflow assistant got a container
#   "claim_idle_container(...): N candidates" — Diagnostic: shows all idle candidates with labels
```

### Checking K8s state

```bash
# All jobs in preview namespace
kubectl get jobs -n preview -l app=unity -o custom-columns='NAME:.metadata.name,STATUS:.metadata.labels.unity-status,AID:.metadata.labels.assistant-id,ACTIVE:.status.active'

# Idle containers only
kubectl get jobs -n preview -l app=unity,unity-status=idle --no-headers

# Check a specific assistant
kubectl get jobs -n preview -l app=unity,assistant-id=862 --no-headers
```

### Checking GCE VM state

```bash
# Idle VMs in preview zone
gcloud compute instances list --project=gcp-project-vms \
  --filter="labels.pool-role=idle AND labels.vm-type=ubuntu AND zone:us-central1-b AND status=RUNNING" \
  --format='table(name)'

# VMs assigned to a specific assistant
gcloud compute instances list --project=gcp-project-vms \
  --filter="labels.assistant-id=862 AND labels.pool-role=assigned" \
  --format='table(name,zone,labels.pool-role)'

# Check VM metadata (hostname, unify-key presence)
gcloud compute instances describe unity-pool-ubuntu-23-preview \
  --zone=us-central1-b --project=gcp-project-vms \
  --format='value(metadata.items[hostname])'
```

### Checking Pub/Sub

```bash
# List pending startup messages (without consuming)
gcloud pubsub subscriptions pull unity-pending-startups-preview-sub \
  --project=gcp-project-runtime --limit=5

# Check subscription config
gcloud pubsub subscriptions describe unity-pending-startups-preview-sub \
  --project=gcp-project-runtime \
  --format='yaml(messageRetentionDuration,ackDeadlineSeconds)'
```

### Manual cleanup between runs

The test now handles cleanup automatically in the Setup phase. If you need to
clean up manually:

```bash
# Delete all preview jobs
kubectl get jobs -n preview -l app=unity --no-headers -o name | xargs kubectl delete -n preview

# Release all VMs for specific assistants
ADMIN_KEY=... && for aid in 862 863 864; do
  curl -s -X POST "https://service.a.run.app/infra/vm/pool/release" \
    -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
    -d "{\"assistant_id\": \"$aid\"}"
done

# Create fresh idle containers
curl -s -X POST "https://service.a.run.app/scheduled/jobs/create" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

---

## 7. Bugs Found and Fixed

### Bug 1: Container boot-time label race (Unity repo)

**Symptom**: Overflow assistants consistently lost their containers. The first
assistant in the pending queue never got served.

**Root cause**: In `comms_manager.py:start()`, idle containers unconditionally
called `mark_job_label("idle")` on boot. If the reconciler had already claimed
the container (setting `unity-status=running`), the daemon thread's delayed
PATCH reset it back to `idle`, causing another reconciler call to reassign it
to a different assistant.

**Evidence**: Cloud Run logs showed `Job labels patched: uf9c3 -> {'unity-status': 'idle'}`
9 seconds after the reconciler set it to `running`.

**Fix**: `comms_manager.py:start()` now reads the job's current labels before
patching. If `unity-status` is already `running`, the idle patch is skipped.

### Bug 2: SETTINGS didn't recognize DEPLOY_ENV=preview

**Symptom**: Preview Comms App claimed containers from the production namespace.
Adapter 500s because it looked up assistants on production Orchestra.

**Root cause**: `common/settings.py` only checked `STAGING=true/false`. Preview
Cloud Run services set `DEPLOY_ENV=preview` (not `STAGING=true`).

**Fix**: `_get_deploy_env()` now checks `DEPLOY_ENV` first, falls back to `STAGING`.

### Bug 3: Hardcoded `-staging` strings

**Symptom**: Preview containers got `-staging` suffixed names and topics instead
of `-preview`.

**Fix**: Replaced all inline conditionals with `SETTINGS.env_suffix`,
`SETTINGS.assistant_topic(id)`, `SETTINGS.unity_image_name`, etc.

---

## 8. Cloud Scheduler Jobs (Preview)

| Job | Schedule | Endpoint | What it does |
|---|---|---|---|
| `jobs-create-preview` | `0 * * * *` (hourly) | `/scheduled/jobs/create?refresh=true` | Creates idle containers with latest image |
| `jobs-cleanup-preview` | `10 * * * *` (HH:10) | `/scheduled/jobs/cleanup` | Deletes excess idle containers |
| `stale-jobs-expire-preview` | `0 1,7,13,19 * * *` | `/scheduled/jobs/expire-stale` | Suspends jobs running >12h, releases VMs |
| `email-watches-preview` | `0 0 * * *` | `/scheduled/email-watches` | Renews Gmail/Outlook watch subscriptions |
| `microsoft-tokens-preview` | `*/30 * * * *` | `/scheduled/microsoft-tokens` | Refreshes Microsoft OAuth tokens |
| `teams-watches-preview` | `*/30 * * * *` | `/scheduled/teams-watches` | Renews Teams chat/channel subscriptions |
| `cert-renewal-preview` | `0 3 1 * *` | `/scheduled/cert-renewal` | Renews *.vm.unify.ai wildcard TLS cert |

---

## 9. File Reference

| File | Purpose |
|---|---|
| `tests/infra/integration/test_stress.py` | The stress test (1000+ lines, 8 phases) |
| `tests/infra/integration/conftest.py` | Shared fixtures, helpers, invariant checker |
| `tests/infra/integration/.env` | Test environment config (gitignored) |
| `communication/infra/helpers.py` | K8s auth, Lease functions, pending-startup queue |
| `communication/infra/views.py` | FastAPI endpoints: /job/start, /pending/process, /vm/pool/* |
| `communication/infra/vm_helpers.py` | GCE VM lifecycle: claim, assign, release, provision |
| `common/settings.py` | Centralized SETTINGS singleton |
| `adapters/helpers.py` | Adapter-side: build_webhook_context, replenish, cleanup |
| `adapters/main.py` | Adapter endpoints, scheduled job handlers |
| `investigations/infra-invariants.md` | 16 infrastructure invariants with failure modes |
| `guides/DEPLOY_CHECKLIST.md` | Deployment steps for the Lease-based migration |
