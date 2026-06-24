# droid-deploy

Private deployment + hosted-runtime repo for the **Droid** AI assistant platform (formerly **Unity**). This repo owns the hosted communication stack, the assistant desktop/VM infrastructure, the full local self-host stack, the client/assistant deployment overlay, and the CI/CD that produces production images.

> **This README is the central source of truth for the fully-deployed system** — every "moving part" (GCP projects, GKE, Cloud Run, VM pool, tunnel/SFTP, Pub/Sub, DNS, secrets, CI/CD) and the current state of the **Unity → Droid rename**. If you are hunting an environment variable, a GCP resource name, a secret, a Cloud Build trigger, or "why is X still called `unity`", it should be answered here. Deep-dive operational guides are linked inline; this document is the index and the authority over them.

---

## Table of contents

1. [The repo set](#1-the-repo-set)
2. [Unity → Droid rename: read this first](#2-unity--droid-rename-read-this-first)
3. [GCP project map](#3-gcp-project-map)
4. [Live resource inventory (canonical names)](#4-live-resource-inventory-canonical-names)
5. [Hosted runtime architecture](#5-hosted-runtime-architecture)
6. [Assistant desktops: VM pool, tunnel/SFTP, archives, TLS](#6-assistant-desktops-vm-pool-tunnelsftp-archives-tls)
7. [Secrets management](#7-secrets-management)
8. [CI/CD: Cloud Build, GitHub Actions, branches](#8-cicd-cloud-build-github-actions-branches)
9. [Known rename loose ends & gotchas](#9-known-rename-loose-ends--gotchas)
10. [The deployment overlay package](#10-the-deployment-overlay-package)
11. [Local development & self-host](#11-local-development--self-host)
12. [Troubleshooting](#12-troubleshooting)
13. [Deep-dive guides](#13-deep-dive-guides)

---

## 1. The repo set

| Repo | Visibility | Role | Branches |
|---|---|---|---|
| [`unifyai/droid`](https://github.com/unifyai/droid) | public | The assistant runtime ("the brain"). Runs as the GKE container and as the desktop runtime. | `main` (prod), `staging` |
| [`unifyai/droid-deploy`](https://github.com/unifyai/droid-deploy) | private | **This repo.** Hosted comms app + adapters, assistant VM/tunnel infra, self-host stack, client deployment overlay, prod image CI/CD. | `main`, `staging`, `stability` |
| [`unifyai/orchestra`](https://github.com/unifyai/orchestra) | private/hosted | Backend API + Postgres (users, assistants, projects, contexts, logs, billing). | `main`, `staging` |
| [`unifyai/console`](https://github.com/unifyai/console) | private/hosted | Next.js web UI / observability dashboard. | `main`, `staging` |
| [`unifyai/unify`](https://github.com/unifyai/unify) | public | Python SDK wrapping Orchestra's REST API. | `main`, `staging` |
| [`unifyai/unillm`](https://github.com/unifyai/unillm) | public | LLM abstraction layer with caching. | `main`, `staging` |
| [`unifyai/magnitude`](https://github.com/unifyai/magnitude) | private | Computer-use/browser automation dependency. **Consumed at branch `unity-modifications`** (see loose ends). | `unity-modifications` |

Open-source `droid` runs against hosted Orchestra (`ORCHESTRA_URL`, default `https://api.unify.ai/v0`) with no Console. The full "all-repo local" stack lives in `selfhost/` here.

**Branch model (all repos):** default `main` = production, `staging` = development/testing. Promotion is **`staging` → `main`** (never feature → `main` directly). A `sync-staging.yml` workflow fast-forwards `staging` after `main` moves. The big exception today: see [loose ends](#9-known-rename-loose-ends--gotchas) for fixes stranded on `staging`.

---

## 2. Unity → Droid rename: read this first

The platform was renamed **Unity → Droid** on **2026-06-18** (one `Rename … to Droid` commit per repo, all merged to both `main` and `staging`). The coordinator persona was separately renamed Unity → **Twin** (Orchestra `8e62c4af`) / **Marty** (Console `db7253df`).

### The mental model that explains every bug

The rename was executed as an **additive alias strategy, not an in-place rename**:

1. **Code/identifiers** were mechanically renamed `unity*` → `droid*` across all six repos.
2. **New `droid-*` GCP/K8s resources** were created *alongside* the existing `unity-*` ones (buckets, Artifact Registry repos, service accounts, K8s aliases, some Pub/Sub, some pool resources).
3. **GitHub repos** were renamed via API (`unity` → `droid`, `unity-deploy` → `droid-deploy`).
4. Several classes of resource were **deliberately or accidentally left as `unity-*`** and the code still points at them.

This is why things broke silently for days: **the code says `droid-*` but the live resource is still `unity-*` (or vice-versa, or the `droid-*` copy was created empty and never cut over).** A symbol mismatch produces a 404 / 401 / empty-config at runtime, not a build error.

### What can never be renamed

- **GCP project IDs are immutable.** `gcp-project-vms` and `gcp-project-runtime` (display name still "Unity LiveKit") will keep the `unity` name forever unless we migrate to brand-new projects. Code intentionally keeps `vm_project_id = "gcp-project-vms"`.
- **Service-account emails** embed the (immutable) project id, so `service-account@example.iam.gserviceaccount.com` stays.
- **GKE cluster names** can't be renamed (recreate-only); the cluster is `unity`. Code is decoupled via `DROID_GKE_CLUSTER_NAME` (default `unity`) / Cloud Build `_CLUSTER: 'unity'`.

### Rename status by layer (as of 2026-06-24)

| Layer | State |
|---|---|
| Code/identifiers (all 6 repos) | ✅ renamed, on `main` + `staging` |
| GKE **workloads** (deployments, cronjobs) | ✅ `droid-*` |
| Cloud Run comms/adapters | ✅ `droid-comms-app`, `droid-adapters` (+ `-staging`) |
| Ubuntu desktop pool (images, VMs, IPs, DNS) | ✅ migrated to `droid-pool-ubuntu-*` |
| Per-assistant archive bucket | ✅ `droid-assistant-archives` (migrated 2026-06-24) |
| GKE **cluster name** | ❌ still `unity` (immutable-ish) |
| Tunnel servers + IPs | ❌ `unity-tunnel-server(-staging)`, `unity-tunnel-server-ip*` |
| Tunnel config bucket | ❌ `unity-tunnel-config(-staging)` (no droid equivalent) |
| **Windows** desktop pool (images, VMs, IPs, DNS) | ❌ entirely `unity-pool-windows-*` |
| Pub/Sub fabric | ⚠️ ~84% still `unity-*` (1,334 topics / 5,198 subs vs 252 / 1,021 droid) |
| Data buckets (recordings, logs, artifacts) | ⚠️ `droid-*` created empty; code partly still reads `unity-*` |
| GitHub org secrets `UNITY_ADAPTERS_URL` / `UNITY_COMMS_URL` | ❌ still `unity`; workflows read `DROID_*` → empty at runtime |
| Secrets `UNITY_COORDINATOR_*`, `UNITY_{LIVEKIT,OPENAI,…}` | ⚠️ dual-written (`DROID_COORDINATOR_*` added; comms/LLM still unity) |
| GCP project IDs / SA emails / cluster name | ❌ immutable — `unity` forever |

The full prioritized list of mismatches that can still bite is in [§9 Known rename loose ends](#9-known-rename-loose-ends--gotchas).

### Key rename commits (cross-reference)

| Commit | Repo | Subject |
|---|---|---|
| `224cf2ae` | orchestra | Rename Unity platform references to Droid. |
| `ed4af7c` | unify | Rename Unity references to Droid. |
| `ca68806` | unillm | Rename Unity references to Droid. |
| `be68c00c2` | droid | Rename Unity runtime to Droid. (whole `unity/` pkg → `droid/`, Dockerfiles, k8s, CI) |
| `6bf17b3e` | droid-deploy | Rename hosted runtime references to Droid. |
| `8037a422` | droid-deploy | Rename deploy repo to Droid deploy. |
| `34daa854` | console | Rename Unity references to Droid. |
| `8b5097a9` | droid-deploy | Fix: point tunnel control plane at existing `unity-tunnel-config` bucket — **STAGING ONLY** |
| `244ad103` | droid-deploy | fix(infra): restore SFTP tunnel band reachability — **STAGING ONLY** |

---

## 3. GCP project map

Four projects, split to isolate workloads and GCE API rate limits.

| Project ID | Display | Role | Region(s) |
|---|---|---|---|
| `gcp-project-runtime` | **Unity LiveKit** | Main hosted runtime: GKE cluster `unity`, Cloud Run (comms/adapters), Pub/Sub fleet, most buckets, tunnel servers, Artifact Registry | `us-central1` |
| `gcp-project-vms` | Unity Assistant VMs | Assistant desktop VM pool: pool images/families, pool VMs, static IPs, per-assistant archives | `us-central1` (`-a`, `-f`) |
| `gcp-project-saas` | SaaS | **Orchestra + Console + landing page** (Cloud Run) and **Cloud SQL** (Postgres) | `europe-west1` / `europe-west3` |
| `gcp-project-dns` | DNS-Server | Public Cloud DNS zone `unifyai` → `unify.ai` (incl. `vm.unify.ai`, `tunnel.unify.ai`) | global |

**Why VMs are a separate project:** GKE Autopilot Node Auto-Provisioning consumes the per-project `compute.instances.insert` rate limit. Co-locating assistant-VM creation with a GKE NAP burst historically exhausted the limit and 500'd the hiring flow. Separating the pool into `gcp-project-vms` gives each its own budget.

---

## 4. Live resource inventory (canonical names)

Authoritative as of 2026-06-24. `⚠️` marks a unity/droid mismatch that the code/runtime depends on.

### 4.1 `gcp-project-runtime` — main runtime

**GKE cluster `unity`** (regional `us-central1`, Autopilot). Namespaces: `production`, `staging` (+ `cert-manager`, `external-secrets`, `custom-metrics`).

Production deployments (image):
- `assistant-session-controller`, `assistant-session-pool-controller` → `droid-comms-app-repo/assistant-session-controller:<sha>`
- `droid-ingest-worker`, `droid-parse-worker` → `droid/droid:latest`
- `job-watcher` → `droid/job-watcher:<sha>`
- `grafana` (+ cloud-sql-proxy)

CronJobs (prod + staging): `droid-failed-pod-gc` (*/10), `droid-pipeline-dlq-reconciler` (*/10), `droid-pipeline-stale-reconciler` (*/15), `droid-workers-weekly-rollout` (Sun 03:00). Assistant Job pods are spawned on demand (label `app=droid`), not a Deployment.

**Cloud Run** (`us-central1`): `droid-comms-app`(+`-staging`), `droid-adapters`(+`-staging`), `link-tracker`, `smartlead-reply`(+`-sandbox`). Prod tag `6376458` = droid-deploy `main`; staging tag `c402d42`.

**Compute:** `unity-tunnel-server`, `unity-tunnel-server-staging` ⚠️ (us-central1-a).

**Static IPs:** `unity-gke-egress-ip` = `203.0.113.12` (Cloud NAT egress; firewall source for SFTP), `unity-tunnel-server-ip` = `203.0.113.13` (= `tunnel.unify.ai`), `unity-tunnel-server-ip-staging` = `136.112.29.250` ⚠️.

**Firewall:** `allow-7000` (tcp:7000, tag `unity-tunnel-server` — rathole control), `allow-tunnel-sftp` (tcp:61000-61999 from `203.0.113.12/32`, tag `allow-tunnel` — SFTP band), `gke-unity-8c75014d-*` (cluster rules).

**GCS buckets:** `unity-tunnel-config`(+`-staging`) ⚠️ (no droid equiv), `unity-youtube-extraction` ⚠️, and duplicated pairs `{unity,droid}-call-recordings`, `{unity,droid}-image-hash`, `{unity,droid}-pipeline-artifacts(-staging)`, `{unity,droid}-pod-logs` ⚠️ (droid copies created, code partly still reads unity).

**Artifact Registry:** `droid`, `droid-comms-app-repo`, `droid-adapters-repo`, `link-tracker-repo` + legacy `unity`, `unity-adapters-repo`, `unity-comms-app-repo`, `unity-livekit-app-repo`.

**Service accounts:** `comm-sa@` (all assistant pods + cluster services run as this), `droid-pipeline-worker@` + legacy `unity-pipeline-worker@`, `link-tracker-sa@`, `external-secrets-reader@`. `comm-sa` IAM is hand-managed; required roles include `roles/monitoring.metricWriter`, `roles/logging.logWriter`, `roles/pubsub.editor`, and `roles/compute.admin` + `roles/secretmanager.admin` in `gcp-project-vms`.

**Pub/Sub:** infra topics `droid-ingest`, `droid-parse`, `droid-dead-letter` (+`-staging`); per-assistant `droid-<id>[-staging]` (new) and `unity-<id>[-staging]` (legacy) ⚠️. ~1,590 topics / ~6,222 subs total, ~84% still `unity-*`.

**Secret Manager (names):** `UNITY_ADAPTERS_URL{,_PREVIEW,_STAGING}`, `UNITY_COMMS_URL{,_PREVIEW,_STAGING}` ⚠️, `DROID_ADAPTERS_URL_{PRODUCTION,STAGING}`, plus provider/integration secrets (`ANTHROPIC_API_KEY`, `LIVEKIT_*`, `TWILIO_*`, `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, `VM_WILDCARD_FULLCHAIN/PRIVKEY`, `gcp-sa-key`, `github-pat`, `DEVBOT_GITHUB_TOKEN`, …). Cluster runtime secret = `droid-secrets` (see §7).

### 4.2 `gcp-project-vms` — desktop VM pool

- **Pool VMs:** Ubuntu = `droid-pool-ubuntu-<N>[-staging]` ✅ (us-central1-f / -a); Windows = `unity-pool-windows-<N>[-staging]` ⚠️.
- **Images/families:** `droid-pool-ubuntu-vm`, `droid-pool-windows-vm` (created 2026-06-24) ✅; legacy `unity-pool-ubuntu-vm`, `unity-pool-windows-vm`, `unity-ubuntu-vm`, `unity-windows-vm` (rollback source). Code: `communication/infra/vm_config.py` → `POOL_UBUNTU_VM_IMAGE_FAMILY = "droid-pool-ubuntu-vm"`, `*_IMAGE_PROJECT = "gcp-project-vms"`.
- **Static IPs:** `droid-pool-ubuntu-ip-<N>[-staging]` ✅; all Windows + preview/staging Ubuntu IPs still `unity-pool-*-ip-*` ⚠️.
- **Buckets:** `droid-assistant-archives` ✅ (per-assistant home `{id}.tar.gz`, ~1.2 GB) + legacy `unity-assistant-archives` (rollback). Code: `POOL_ASSISTANT_ARCHIVE_BUCKET = "droid-assistant-archives"`.
- **SA:** `pool-vm-sa@gcp-project-vms` (objectAdmin on both archive buckets). Pool/desktop OS user is **`unityuser`** (HOME `/Droid`) ⚠️.
- **Secrets:** `VM_WILDCARD_FULLCHAIN`, `VM_WILDCARD_PRIVKEY`, `DEVBOT_GITHUB_TOKEN`.

### 4.3 `gcp-project-saas` — Orchestra + Console + DB

- **Cloud SQL (Postgres, europe-west3):** `prod-ssd` (`34.40.62.164`), `staging-ssd` (`34.89.176.17`).
- **Cloud Run (europe-west1 unless noted):** `orchestra`(+`-staging`), `saas-web-app` (Console prod), `saas-web-app-redesign-staging` (Console staging, us-central1), `landing-page`(+`-staging`).
- **Secrets delta:** `UNITY_COORDINATOR_*` and `DROID_COORDINATOR_*` both exist (DISCORD_ID/TOKEN, EMAIL_ADDRESS, PHONE_UK/US, WHATSAPP_NUMBER × PRODUCTION/STAGING) ⚠️; `UNITY_{LIVEKIT,OPENAI,DEEPGRAM,CARTESIA}_*` and `UNITY_{ADAPTERS,COMMS}_URL*` still unity-only ⚠️.

### 4.4 `gcp-project-dns` — public DNS

- Zone `unifyai` → `unify.ai.` (93 records). `api/console/unify.ai` → `203.0.113.16` (saas `public-lb-ip`). `tunnel.unify.ai` + `*.tunnel.unify.ai` → `203.0.113.13`; `staging.tunnel…` → `136.112.29.250`.
- `*.vm.unify.ai`: Ubuntu prod hosts `droid-pool-ubuntu-N.vm.unify.ai` ✅; Windows pool + most staging/preview hosts still `unity-*.vm.unify.ai` ⚠️. Liveview URLs: `https://droid-pool-<...>.vm.unify.ai/desktop/custom.html`.

---

## 5. Hosted runtime architecture

The hosted system spans three code areas: **Orchestra** (API + DB), **droid-deploy** (this repo: comms app, adapters, infra controllers), and **droid** (the per-job container on GKE).

### External services

| Service | Purpose |
|---|---|
| Twilio | Phone calls, SMS, WhatsApp |
| Gmail API / Microsoft Graph | Email (Gmail / Outlook) + Teams |
| LiveKit | Real-time audio/video (Unify Meet) |
| Google Cloud Pub/Sub | Message routing adapters ↔ containers |
| GKE (Autopilot) | Container orchestration for Droid jobs |

### Components

- **Adapters** (`adapters/`, Cloud Run `droid-adapters`): unauthenticated webhook handlers for inbound Twilio/Gmail/Microsoft, plus `/scheduled/*` cron endpoints and `/assistant/wakeup`.
- **Comms app** (`communication/`, Cloud Run `droid-comms-app`): admin-key-protected JSON API for outbound phone/SMS/email and the **infra control plane** (`/infra/pubsub/topic`, `/infra/gke/job`, `/infra/tunnel/register`, VM/session management). The same image also runs the GKE `assistant-session-controller` / `assistant-session-pool-controller`.
- **Droid container** (GKE job, image `droid/droid`): the assistant runtime. `CommsManager` subscribes to Pub/Sub; `ConversationManager` orchestrates; `EventBroker` is the in-memory bus; `debug_logger.py` (a job tracker, despite the name) records liveness to the `AssistantJobs` Unify project.

### Pub/Sub & container lifecycle

- Per-assistant topic `droid-{assistant_id}[-staging]` (+ `-sub`). Startup topic `droid-startup[-staging]` engages idle containers.
- Message shape: `{"thread": "<type>", "event": {…}}` with threads `startup`, `msg`, `email`, `call`, `unify_message`, `unify_meet`, `unify_message_outbound`, `assistant_update`.
- **Idle → Live:** idle containers (`agent_id is None`) subscribe to the startup topic and self-ping every 30s. On inbound, the adapter checks `AssistantJobs`, publishes a `startup` message (if not live) + the inbound to the assistant topic. The container that wins the startup subscribes to its assistant topic and marks itself live. Inactivity timeout = **7 min**; the job is retained (not deleted) for logs.

### Idle pool management

Target idle count: `max(DROID_MIN_IDLE_JOBS, live_count // DROID_IDLE_JOB_DEMAND_FACTOR)` (defaults 3 and 5). Three mechanisms share it:
1. **Reactive fill** — on every inbound that consumes an idle job (`/scheduled/jobs/create`).
2. **Deploy refresh** — every Cloud Build (`?refresh=true`) rotates the pool to the new image.
3. **Hourly cron** — self-heal + rotation; cleanup 10 min later trims to target.

### Job-watcher (crash-safe cleanup)

A single-replica [kopf](https://kopf.dev/) operator (`base/scripts/job-watcher/`, image `droid/job-watcher`) watches pods `app=droid`. On `Succeeded`/`Failed` it sets `running=False` in `AssistantJobs` and releases the assigned pool VM — externally, so cleanup happens even if the container crashes (OOM/node failure). The in-container `mark_job_done()` (graceful exit) and the adapters' `expire_all_stale_jobs()` (periodic sweep) call the same idempotent operations.

For the per-pod 10Gi ephemeral-storage Autopilot cap and the `/tmp` `emptyDir` strategy (HuggingFace/Docling caches, `HF_HOME`, `XDG_CACHE_HOME`), see [`deploy/guides/GKE_EPHEMERAL_STORAGE.md`](deploy/guides/GKE_EPHEMERAL_STORAGE.md).

---

## 6. Assistant desktops: VM pool, tunnel/SFTP, archives, TLS

Each live assistant can get a dedicated **desktop VM** (Ubuntu or Windows) from a warm pool in `gcp-project-vms`. **This is the subsystem most damaged by the rename** — it caused the 2026-06-24 production outage.

### Pool VMs & images

- Controllers (`assistant-session-controller`) provision pool VMs from image family `droid-pool-ubuntu-vm` / `droid-pool-windows-vm` in project `gcp-project-vms`, attaching `droid-pool-ubuntu-ip-<N>` and creating `droid-pool-ubuntu-<N>.vm.unify.ai` DNS.
- Images are built by `scripts/vm-build/build-ubuntu.sh` / `build-windows.sh` (+ packer under `communication/infra/scripts/*-vm-custom-image/`), which publish to the `droid-pool-*` families. **If the family doesn't exist in GCP, every provision 404s** (the outage — see [§9](#9-known-rename-loose-ends--gotchas)).

### File sync (home filesystem persistence)

- The runtime syncs the assistant's workspace (`~/Droid/Local`, Attachments/Outputs/functions) to the desktop VM over **rclone SFTP** (`droid/file_manager/sync/`, user `unityuser`, port 2222).
- For users tunnelling a local machine, the **tunnel control plane** (`/infra/tunnel/register` in the comms app) manages a rathole relay on `unity-tunnel-server`, storing state in `gs://bucket`. SFTP uses the raw-TCP band `61000-61999` (firewall `allow-tunnel-sftp` from `unity-gke-egress-ip`). Tunnel constants live in `common/settings.py` (`tunnel_vm_name`, `tunnel_gcs_bucket`) and `communication/infra/tunnel_config.py`.

### Per-session archive (cross-session persistence)

- On release, the desktop's home is tarred to `gs://bucket/{assistant_id}.tar.gz`; on next assignment it's restored. Bucket name is `POOL_ASSISTANT_ARCHIVE_BUCKET` (`vm_config.py`) and is passed to each VM as the `archive-bucket` metadata key, used by `droid-pool-watcher.sh`. **If the bucket doesn't exist, files don't persist between sessions** (a rename gap fixed 2026-06-24).

### VM TLS (wildcard)

Every VM runs Caddy terminating TLS for `/api/*` (agent-service) and `/desktop/*` (noVNC). A single `*.vm.unify.ai` wildcard cert (`VM_WILDCARD_FULLCHAIN` / `VM_WILDCARD_PRIVKEY` in `gcp-project-vms` Secret Manager) is pushed to each VM via instance metadata (avoids Let's Encrypt's 50 certs/domain/week limit). Renewed monthly by the `cert-renewal` Cloud Scheduler job → `POST /scheduled/cert-renewal` (DNS-01 in the `unifyai` zone).

---

## 7. Secrets management

There are **three independent secret planes** — rotating one does not rotate the others.

### 7.1 GCP Secret Manager → GKE (`droid-secrets`)

Source of truth for runtime API keys is **GCP Secret Manager in `gcp-project-runtime`**. The **External Secrets Operator** (namespace `external-secrets`) polls SM `latest` into the `droid-secrets` K8s Secret per namespace (`refreshInterval: 1h`) via `ClusterSecretStore` `droid-gcp-secret-manager`. Droid Job pods read keys with `secretKeyRef`.

Rotate by adding a new SM **version**, then force sync + restart pods:
```bash
kubectl annotate externalsecret droid-secrets -n staging force-sync=$(date +%s) --overwrite
# then trigger a pool refresh so new pods pick it up
```
Bootstrap, break-glass (`setup_k8s_config.py`), and ESO details: [`deploy/guides/DROID_CLUSTER_SECRETS.md`](deploy/guides/DROID_CLUSTER_SECRETS.md). Never `kubectl apply` hand-built secret YAML; never commit key material.

### 7.2 GitHub Actions secrets (CI)

Org `unifyai` secrets are inherited by all repos: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLONE_TOKEN`, `GCP_SERVICE_ACCOUNT_JSON`, `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, and ⚠️ `UNITY_ADAPTERS_URL`, `UNITY_COMMS_URL`. Org variables: `GCP_PROJECT_ID=gcp-project-saas`, `GCP_LOCATION=europe-west1`, `GCP_BUCKET_*`. Per-repo additions are listed in [§8](#8-cicd-cloud-build-github-actions-branches). **CI mismatch:** workflows now read `DROID_COMMS_URL`/`DROID_ADAPTERS_URL` but only `UNITY_*` exist → empty at runtime (see [§9](#9-known-rename-loose-ends--gotchas)).

### 7.3 VM TLS secrets

`VM_WILDCARD_FULLCHAIN` / `VM_WILDCARD_PRIVKEY` in `gcp-project-vms` Secret Manager (see §6).

---

## 8. CI/CD: Cloud Build, GitHub Actions, branches

### Cloud Build (image build + GKE deploy)

Triggers fire on branch pushes; the GitHub connection is `github-unifyai` (`gcp-project-runtime/us-central1`).

| Branch | Trigger | Image | Env |
|---|---|---|---|
| `staging` | `droid-deploy-staging` | `droid-staging` | Staging |
| `main` | `droid-deploy-production` | `droid` | Production |

Each build clones `droid` (matching branch), overlays this package, pushes to Artifact Registry, updates the GCS image hash, applies `ExternalSecret` manifests, and refreshes the GKE idle pool. Cloud Build config retains `_UNITY_REF`/`_UNITY_SHA` **fallback** substitutions and `_CLUSTER: 'unity'`. ⚠️ Some legacy triggers are still bound to `repositories/unity` / `unity-deploy` and fail at source-fetch — see [§9](#9-known-rename-loose-ends--gotchas).

### GitHub Actions (per repo)

- **All repos:** `tests.yml`, `sync-staging.yml` (fast-forward `staging` after `main`).
- **orchestra:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/orchestra`), billing/cleanup cron jobs hitting `https://api.unify.ai/v0/admin/*` (prod) and `https://internal.example.com/v0/*` (staging).
- **droid:** `tests.yml` (env `droid-testing`; clones orchestra/unify/unillm + `magnitude@unity-modifications`), `llm-cache-refresh.yml` (env `droid-llm-cache-refresh`), `pages.yml`.
- **droid-deploy:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/droid-selfhost`, `droid-desktop-selfhost`; `workflow_dispatch` only), `hosted-tests.yml` (`GCP_PROJECT_ID=gcp-project-runtime`).
- **console:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/console-selfhost`), `code-quality.yml`, `security.yml`.
- **unillm:** `pypi.yml` (publish on tag).

### Branch promotion

Land changes on `staging`, let staging deploy/validate, then promote `staging` → `main`. Never merge a feature branch directly into `main`/`master`.

---

## 9. Known rename loose ends & gotchas

Prioritized. `P0` = can break production, `P1` = breaks CI / partial degradation, `P2` = cleanup/clarity.

| # | Pri | Item | Detail / fix |
|---|---|---|---|
| 1 | **P0** | Tunnel fixes stranded on `staging` | `8b5097a9` (tunnel bucket/VM → `unity-*`) and `244ad103` (SFTP firewall band) are on `origin/staging` **not `origin/main`**. Prod `common/settings.py` may still point tunnel at non-existent `droid-tunnel-*`. **Merging `staging`→`main` is required** for these (but does **not** fix #2/#3 which are identical on both branches). |
| 2 | **P0** | Pool image families (RESOLVED 2026-06-24) | Code wanted `droid-pool-ubuntu-vm`; only `unity-pool-ubuntu-vm` existed → 404 on every desktop provision → `assistant-session-controller` CrashLoop. Fixed by creating `droid-pool-*` families. **Keep the families fresh:** `build-ubuntu.sh`/`build-windows.sh` publish to them. |
| 3 | **P0** | Archive bucket (RESOLVED 2026-06-24) | Code wanted `droid-assistant-archives`; only `unity-assistant-archives` existed → no cross-session file persistence. Fixed by bucket create + rsync (183 objects). |
| 4 | **P1** | CI URL name split | Workflows read `DROID_COMMS_URL`/`DROID_ADAPTERS_URL`; only org secrets `UNITY_COMMS_URL`/`UNITY_ADAPTERS_URL` exist → empty at runtime in `droid-deploy/hosted-tests.yml`, `droid/tests.yml`, `droid/llm-cache-refresh.yml`. Rename the org secrets (or add the `DROID_*` repo vars/secrets). |
| 5 | **P1** | Orphaned GitHub environments | `droid` repo has `unity-testing` (full secret set) and `unity-llm-cache-refresh` superseded by `droid-testing` / `droid-llm-cache-refresh`. Migrate env-scoped secrets + delete the `unity-*` envs. |
| 6 | **P1** | Stale Cloud Build triggers | Triggers bound to `repositories/unity` / `unity-deploy` (e.g. `adapters-unity-deploy`, `unity-comms-app-*`) fail at source-fetch (~3-6s, no steps) even though GitHub redirects the repo. Recreate as `droid-*` triggers against `repositories/droid` / `droid-deploy`. Compat files `cloudbuild/unity-comms-app*.yaml` exist for old trigger names. |
| 7 | **P1** | Referenced-but-unconfigured secrets | `PYPI_API_TOKEN` (unillm `pypi.yml`), `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` (console build-arg), `TWILIO_*`/`LIVEKIT_*`/`GCP_SA_KEY` (droid-deploy `hosted-tests.yml`) are not configured → empty/skip. |
| 8 | **P2** | `magnitude@unity-modifications` | `droid` `install.sh`/CI and droid-deploy `droid-pool-watcher.sh`/`cloud-bootstrap.sh` fetch the `unity-modifications` branch of `unifyai/magnitude`. Rename the branch + update refs as a coordinated change. |
| 9 | **P2** | OS user `unityuser` (HOME `/Droid`) | Across droid desktop scripts + droid-deploy packer/pool scripts + `file_manager/sync/config.py`. Renaming requires rebuilding pool images. |
| 10 | **P2** | Windows pool entirely `unity-*` | Images, VMs, IPs, DNS for Windows desktops not migrated (Ubuntu done). They survive because long-lived; will break on next Windows image rebuild/replenish. |
| 11 | **P2** | Pub/Sub ~84% `unity-*` | 1,334 topics / 5,198 subs legacy vs 252 / 1,021 droid; per-assistant duplication. Cut over + delete legacy. |
| 12 | **P2** | Empty `droid-*` data buckets | `droid-call-recordings`, `droid-pod-logs`, `droid-pipeline-artifacts`, `droid-image-hash` created empty; code partly still reads/writes `unity-*` (which hold the data). `unity-tunnel-config`/`unity-youtube-extraction` have no droid copy. |
| 13 | **P2** | Duplicate service accounts | `unity-pipeline-worker` + `droid-pipeline-worker` both exist. |
| 14 | **P2** | App-level `unity` string constants | `UnityTests` (orchestra context-cleanup + default test project across repos), `UnitySystemEvent` gateway envelope (droid ↔ console wire contract), `WaitingForUnity` session-state labels (droid-deploy controller), `unity-user-filesync` SSH key comment (orchestra). These are deliberate cross-references, not typos — rename only with coordinated multi-repo changes. |
| 15 | **P2** | Rollback resources to clean up | Legacy `unity-pool-*` images and `unity-assistant-archives` were kept as rollback after the 2026-06-24 fix. Delete once a real session confirms restore against `droid-assistant-archives`. |
| 16 | — | Immutable `unity` names | Project IDs `gcp-project-vms` / `gcp-project-runtime` (display "Unity LiveKit"), SA emails, GKE cluster `unity`. Accept as canonical; do **not** attempt to "fix" in code (it already targets these intentionally). |

---

## 10. The deployment overlay package

`droid_deploy/` is the enterprise overlay loaded by `droid` at runtime via a Python entry point. When `_DROID_STARTUP_HOOK_GROUP` is set (K8s Secret in hosted deploys), `droid` calls `importlib.metadata.entry_points()` and runs this package's `startup_hook()`. Absent that env var (open-source), the mechanism is inert.

The startup hook:
1. **Resolves the assistant deployment** — deployment-matched spec with org/team/user/assistant seed layers merged in scope order, plus `.secrets.json`.
2. **Syncs seed data** — hash-based idempotent sync of contacts, guidance, knowledge, secrets, blacklist to the Unify backend.
3. **Syncs custom functions** — upserts client memoized functions + venvs via `FunctionManager.sync_custom()`.

```
droid_deploy/
├── hook.py                       # entry point: startup_hook()
└── assistant_deployments/
    ├── clients/                  # client_alpha/, clientgamma/, clientzeta/, …  (self-register)
    ├── configs/types/            # ActorConfig
    ├── environments/             # serialized environment reconstruction
    ├── seed_sync.py              # generic hash-based seed sync
    └── secrets_file.py           # .secrets.json parser
```

**Adding a client:** create `clients/<name>/`, define `deployments/<name>/` (each exposes a `DeploymentSpec` via `get_deployment()`), build an `EnvironmentConfig`/`DeploymentMapping` and call `register_client()` in `__init__.py`, import the client at the bottom of `clients/__init__.py`, and add any runtime secrets to `.secrets.json` (gitignored).

> **`base/` migration note:** `base/` is the private mirror of hosted base-image assets that still live in `droid/deploy/` today. Treat `base/` as the canonical private copy while live triggers run from `droid`; cut over only after the private path is verified end-to-end.

---

## 11. Local development & self-host

Setup:
```bash
git clone git@github.com:unifyai/droid.git
git clone git@github.com:unifyai/unify.git
git clone git@github.com:unifyai/unillm.git
git clone git@github.com:unifyai/droid-deploy.git
cd droid-deploy && uv sync --all-groups && pre-commit install
```
First-party deps resolve from sibling editable checkouts. Run `pre-commit install` in every fresh checkout/worktree.

The full all-repo local stack (local Orchestra + Console + Coordinator + gateway) is in `selfhost/` — start with `bash selfhost/stack.sh up --durable`, inspect with `selfhost/stack.sh status`, reset with `selfhost/stack.sh reset`. The inner-loop runbook is [`docs/local-full-stack-inner-loop.md`](docs/local-full-stack-inner-loop.md). LiveKit compose: [`deploy/selfhost/LIVEKIT_COMPOSE.md`](deploy/selfhost/LIVEKIT_COMPOSE.md).

---

## 12. Troubleshooting

| Symptom | Likely cause | Debug |
|---|---|---|
| Assistant not responding | No idle container | GKE for idle jobs; `/scheduled/jobs/create` logs |
| Delayed response (minutes) | Startup queued, no idle container | Check idle jobs; trigger job creation |
| **Files vanish between sessions / "workspace was wiped"** | Desktop VM not provisioned (image family 404) or archive bucket missing | `kubectl logs deploy/assistant-session-controller -n production` for `image … was not found`; confirm `droid-pool-ubuntu-vm` family + `droid-assistant-archives` bucket exist |
| `assistant-session-controller` CrashLoop (Exit 137) | Provision loop stalls health probe (usually image-family 404) | Same as above; verify pool VMs are `RUNNING` in `gcp-project-vms` |
| `/infra/tunnel/register` 500 | Tunnel bucket name mismatch (`droid-tunnel-config` doesn't exist) | Ensure code points at `unity-tunnel-config` (loose end #1) |
| `/infra/tunnel/register` 401 | Orchestra `/user/basic-info` rejecting the key | Auth issue in tunnel control plane (separate from #1) |
| Email/SMS not received | Watch expired / contact not validated | `/scheduled/email-watches` logs; verify saved contact |
| `TLSV1_ALERT_INTERNAL_ERROR` on desktop | VM missing wildcard cert | `VM_WILDCARD_FULLCHAIN` in `gcp-project-vms`; `crt.sh/?q=%.vm.unify.ai` |
| CI deploy reads empty COMMS/ADAPTERS URL | Secret name split (#4) | Org has `UNITY_*`; workflow reads `DROID_*` |
| Cloud Build fails instantly, no steps | Stale trigger on `repositories/unity[-deploy]` (#6) | Recreate trigger against `droid`/`droid-deploy` |
| Hiring 500 / "Rate Limit Exceeded" | GCE `instances.insert` rate limit | `gcloud logging read` in the VM project for 403s |

---

## 13. Deep-dive guides

| Guide | Topic |
|---|---|
| [`deploy/guides/DROID_CLUSTER_SECRETS.md`](deploy/guides/DROID_CLUSTER_SECRETS.md) | ESO bootstrap, `droid-secrets` rotation, break-glass |
| [`deploy/guides/GKE_EPHEMERAL_STORAGE.md`](deploy/guides/GKE_EPHEMERAL_STORAGE.md) | 10Gi Autopilot cap, `emptyDir` `/tmp`, HF/Docling caches |
| [`deploy/guides/TELEMETRY.md`](deploy/guides/TELEMETRY.md) | Prometheus / Cloud Monitoring metrics pipeline |
| [`deploy/guides/CALL_RECORDING.md`](deploy/guides/CALL_RECORDING.md) | Call recording storage + flow |
| [`deploy/communication/guides/DEPLOY_CHECKLIST.md`](deploy/communication/guides/DEPLOY_CHECKLIST.md) | Comms deploy checklist |
| [`docs/coordinator-onboarding-contract.md`](docs/coordinator-onboarding-contract.md) | Coordinator onboarding contract |
| [`guides/LOCAL_ASSISTANTS.md`](guides/LOCAL_ASSISTANTS.md) | Local assistant deployments |

> Maintenance: when infrastructure changes, update **this README first**. It supersedes the former `deploy/guides/INFRA.md` (folded in 2026-06). Keep the deep-dive guides for operational procedure; keep architecture + canonical names here.
