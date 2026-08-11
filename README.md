# unity-deploy

Private deployment + hosted-runtime repo for the **Unity** AI assistant platform (formerly **Unity**). This repo owns the hosted communication stack, the assistant desktop/VM infrastructure, the full local self-host stack, the client/assistant deployment overlay, and the CI/CD that produces production images.

> **This README is the central source of truth for the fully-deployed system** — every "moving part" (GCP projects, GKE, Cloud Run, VM pool, tunnel/SFTP, Pub/Sub, DNS, secrets, CI/CD) and the current state of the **Unity → Unity rename**. If you are hunting an environment variable, a GCP resource name, a secret, a Cloud Build trigger, or "why is X still called `unity`", it should be answered here. Deep-dive operational guides are linked inline; this document is the index and the authority over them.

---

## Table of contents

1. [The repo set](#1-the-repo-set)
2. [Unity → Unity rename: read this first](#2-unity--unity-rename-read-this-first)
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
| [`unifyai/unity`](https://github.com/unifyai/unity) | public | The assistant runtime ("the brain"). Runs as the GKE container and as the desktop runtime. | `main` (prod), `staging` |
| [`unifyai/unity-deploy`](https://github.com/unifyai/unity-deploy) | private | **This repo.** Hosted comms app + adapters, assistant VM/tunnel infra, self-host stack, client deployment overlay, prod image CI/CD. | `main`, `staging`, `stability` |
| [`unifyai/orchestra`](https://github.com/unifyai/orchestra) | private/hosted | Backend API + Postgres (users, assistants, projects, contexts, logs, billing). | `main`, `staging` |
| [`unifyai/console`](https://github.com/unifyai/console) | private/hosted | Next.js web UI / observability dashboard. | `main`, `staging` |
| [`unifyai/unify`](https://github.com/unifyai/unify) | public | Python SDK wrapping Orchestra's REST API. | `main`, `staging` |
| [`unifyai/unillm`](https://github.com/unifyai/unillm) | public | LLM abstraction layer with caching. | `main`, `staging` |
| [`unifyai/magnitude`](https://github.com/unifyai/magnitude) | private | Computer-use/browser automation dependency. **Consumed at branch `main`**. | `main` |

Open-source `unity` runs against hosted Orchestra (`ORCHESTRA_URL`, default `https://api.unify.ai/v0`) with no Console. The full "all-repo local" stack lives in `selfhost/` here.

**Branch model (all repos):** default `main` = production, `staging` = development/testing. Promotion is **`staging` → `main`** (never feature → `main` directly). A `sync-staging.yml` workflow fast-forwards `staging` after `main` moves. The big exception today: see [loose ends](#9-known-rename-loose-ends--gotchas) for fixes stranded on `staging`.

---

## 2. Unity → Unity rename: read this first

The platform was renamed **Unity → Unity** on **2026-06-18** (one `Rename … to Unity` commit per repo, all merged to both `main` and `staging`). The coordinator persona was separately renamed Unity → **Twin** (Orchestra `8e62c4af`) / **Marty** (Console `db7253df`).

### The mental model that explains every bug

The rename was executed as an **additive alias strategy, not an in-place rename**:

1. **Code/identifiers** were mechanically renamed `unity*` → `unity*` across all six repos.
2. **New `unity-*` GCP/K8s resources** were created *alongside* the existing `unity-*` ones (buckets, Artifact Registry repos, service accounts, K8s aliases, some Pub/Sub, some pool resources).
3. **GitHub repos** were renamed via API (`unity` → `unity`, `unity-deploy` → `unity-deploy`).
4. Several classes of resource were **deliberately or accidentally left as `unity-*`** and the code still points at them.

This is why things broke silently for days: **the code says `unity-*` but the live resource is still `unity-*` (or vice-versa, or the `unity-*` copy was created empty and never cut over).** A symbol mismatch produces a 404 / 401 / empty-config at runtime, not a build error.

### What can never be renamed

- **GCP project IDs are immutable.** `gcp-project-vms` and `gcp-project-runtime` (display name still "Unity LiveKit") will keep the `unity` name forever unless we migrate to brand-new projects. Code intentionally keeps `vm_project_id = "gcp-project-vms"`.
- **Service-account emails** embed the (immutable) project id, so `service-account@example.iam.gserviceaccount.com` stays.
- **GKE cluster names** can't be renamed (recreate-only); the cluster is `unity`. Code is decoupled via `UNITY_GKE_CLUSTER_NAME` (default `unity`) / Cloud Build `_CLUSTER: 'unity'`.

### Rename status by layer (as of 2026-06-24)

| Layer | State |
|---|---|
| Code/identifiers (all 6 repos) | ✅ renamed, on `main` + `staging` |
| GKE **workloads** (deployments, cronjobs) | ✅ `unity-*` |
| Cloud Run comms/adapters | ✅ `unity-comms-app`, `unity-adapters` (+ `-staging`) |
| Ubuntu desktop pool (images, VMs, IPs, DNS) | ✅ migrated to `unity-pool-ubuntu-*` |
| Per-assistant archive bucket | ✅ `unity-assistant-archives` (migrated 2026-06-24) |
| GKE **cluster name** | ❌ still `unity` (immutable-ish) |
| Tunnel servers + IPs | ❌ `unity-tunnel-server(-staging)`, `unity-tunnel-server-ip*` |
| Tunnel config bucket | ❌ `unity-tunnel-config(-staging)` (no unity equivalent) |
| **Windows** desktop pool (images, VMs, IPs, DNS) | ❌ entirely `unity-pool-windows-*` |
| Pub/Sub fabric | ⚠️ ~84% still `unity-*` (1,334 topics / 5,198 subs vs 252 / 1,021 unity) |
| Data buckets (recordings, logs, artifacts) | ⚠️ `unity-*` created empty; code partly still reads `unity-*` |
| GitHub org secrets `UNITY_ADAPTERS_URL` / `UNITY_COMMS_URL` | ❌ still `unity`; workflows read `UNITY_*` → empty at runtime |
| Secrets `UNITY_COORDINATOR_*`, `UNITY_{LIVEKIT,OPENAI,…}` | ⚠️ dual-written (`UNITY_COORDINATOR_*` added; comms/LLM still unity) |
| GCP project IDs / SA emails / cluster name | ❌ immutable — `unity` forever |

The full prioritized list of mismatches that can still bite is in [§9 Known rename loose ends](#9-known-rename-loose-ends--gotchas).

### Key rename commits (cross-reference)

| Commit | Repo | Subject |
|---|---|---|
| `224cf2ae` | orchestra | Rename Unity platform references to Unity. |
| `ed4af7c` | unify | Rename Unity references to Unity. |
| `ca68806` | unillm | Rename Unity references to Unity. |
| `be68c00c2` | unity | Rename Unity runtime to Unity. (whole `unity/` pkg → `unity/`, Dockerfiles, k8s, CI) |
| `6bf17b3e` | unity-deploy | Rename hosted runtime references to Unity. |
| `8037a422` | unity-deploy | Rename deploy repo to Unity deploy. |
| `34daa854` | console | Rename Unity references to Unity. |
| `8b5097a9` | unity-deploy | Fix: point tunnel control plane at existing `unity-tunnel-config` bucket — **STAGING ONLY** |
| `244ad103` | unity-deploy | fix(infra): restore SFTP tunnel band reachability — **STAGING ONLY** |

---

## 3. GCP project map

Four projects, split to isolate workloads and GCE API rate limits.

| Project ID | Display | Role | Region(s) |
|---|---|---|---|
| `gcp-project-runtime` | **Unity LiveKit** | Main hosted runtime: GKE cluster `unity`, Cloud Run (comms/adapters), Pub/Sub fleet, most buckets, tunnel servers, Artifact Registry | `us-central1` |
| `gcp-project-vms` | Unity Assistant VMs | Assistant desktop VM pool: pool images/families, pool VMs, static IPs, per-assistant archives | `us-central1` (`-a`, `-f`) |
| `gcp-project-saas` | SaaS | **Orchestra + Console + landing page** (Cloud Run) and **Cloud SQL** (Postgres) | `us-central1` |
| `gcp-project-dns` | DNS-Server | Public Cloud DNS zone `unifyai` → `unify.ai` (incl. `vm.unify.ai`, `tunnel.unify.ai`) | global |

**Why VMs are a separate project:** GKE Autopilot Node Auto-Provisioning consumes the per-project `compute.instances.insert` rate limit. Co-locating assistant-VM creation with a GKE NAP burst historically exhausted the limit and 500'd the hiring flow. Separating the pool into `gcp-project-vms` gives each its own budget.

### ⚠️ gcloud region/zone cheat-sheet — read this before running any `gcloud` command

Almost everything here is **regional or zonal**, and several `gcloud` surfaces **default to the wrong location and return stale or empty results without erroring**. Mis-reading that output ("the build never ran", "that service doesn't exist", "wrong project") is the single most common recurring mistake. **Always pass the location flag from the table below.**

**The #1 trap — Cloud Build is regional and the `global` default lies.** Every trigger and build for **both** `gcp-project-runtime` and `gcp-project-saas` lives in **`us-central1`**. With no `--region`, `gcloud builds …` queries `global`, where:
- `gcp-project-runtime` → **empty / months-stale** (you'll see only old builds and wrongly conclude the push didn't trigger anything).
- `gcp-project-saas` → shows **only `landing-page`**, silently hiding every `orchestra` and `console` build.

✅ Always: `gcloud builds list --project=<P> --region=us-central1 …` — and the same `--region=us-central1` on `gcloud builds describe`, `gcloud builds log`, `gcloud builds triggers list`, and `gcloud builds triggers run`.

| Resource | gcloud surface | Required location flag |
|---|---|---|
| **Cloud Build** — all triggers + builds, **both** projects | `gcloud builds …` | **`--region=us-central1`** (never `global`) |
| GKE cluster `unity` (`gcp-project-runtime`) | `gcloud container clusters …` | `--region=us-central1` |
| Cloud Run comms/adapters/link-tracker (`gcp-project-runtime`) | `gcloud run …` | `--region=us-central1` |
| Cloud Run `orchestra`(+`-staging`), `landing-page`(+`-staging`), `saas-web-app` (Console **prod**) (`gcp-project-saas`) | `gcloud run …` | `--region=us-central1` |
| Cloud Run `saas-web-app-redesign-staging` (Console **staging**) (`gcp-project-saas`) | `gcloud run …` | `--region=us-central1` ⚠️ (differs from Console prod) |
| Cloud SQL `prod-ssd-usc1` / `staging-ssd-usc1` (`gcp-project-saas`) | `gcloud sql …` / proxy | `us-central1` |
| Compute tunnel VMs (`gcp-project-runtime`) | `gcloud compute …` | zone `us-central1-a` |
| Pool VMs (`gcp-project-vms`) | `gcloud compute …` | zones `us-central1-f` (Ubuntu) / `-a` |
| Cloud DNS zone `unifyai` (`gcp-project-dns`) | `gcloud dns …` | global (no flag) |
| Secret Manager (all projects) | `gcloud secrets …` | global — `--project` only, no region |

The org GitHub variable `GCP_LOCATION=us-central1` is the saas **Cloud Run** default. As of the July 2026 consolidation the **entire** estate — runtime, saas Cloud Run, Cloud SQL, Cloud Build — is in `us-central1`; only Secret Manager and Cloud DNS remain global. When a resource "doesn't exist" or a build "is missing", suspect a wrong/`global` location **before** suspecting a wrong project.

---

## 4. Live resource inventory (canonical names)

Authoritative as of 2026-06-24. `⚠️` marks a unity/unity mismatch that the code/runtime depends on.

### 4.1 `gcp-project-runtime` — main runtime

**GKE cluster `unity`** (regional `us-central1`, Autopilot). Namespaces: `production`, `staging` (+ `cert-manager`, `external-secrets`, `custom-metrics`).

Production deployments (image):
- `assistant-session-controller`, `assistant-session-pool-controller` → `unity-comms-app-repo/assistant-session-controller:<sha>`
- `unity-ingest-worker`, `unity-parse-worker` → `unity/unity:latest`
- `job-watcher` → `unity/job-watcher:<sha>`
- `grafana` (+ cloud-sql-proxy)

CronJobs (prod + staging): `unity-failed-pod-gc` (*/10), `unity-pipeline-dlq-reconciler` (*/10), `unity-pipeline-stale-reconciler` (*/15), `unity-workers-weekly-rollout` (Sun 03:00). Assistant Job pods are spawned on demand (label `app=unity`), not a Deployment.

**Cloud Run** (`us-central1`): `unity-comms-app`(+`-staging`), `unity-adapters`(+`-staging`), `link-tracker`, `smartlead-reply`(+`-sandbox`). Prod tag `6376458` = unity-deploy `main`; staging tag `c402d42`.

**Compute:** `unity-tunnel-server`, `unity-tunnel-server-staging` ⚠️ (us-central1-a).

**Static IPs:** `unity-gke-egress-ip` = `203.0.113.12` (Cloud NAT egress; firewall source for SFTP), `unity-tunnel-server-ip` = `203.0.113.13` (= `tunnel.unify.ai`), `unity-tunnel-server-ip-staging` = `136.112.29.250` ⚠️.

**Firewall:** `allow-7000` (tcp:7000, tag `unity-tunnel-server` — rathole control), `allow-tunnel-sftp` (tcp:61000-61999 from `203.0.113.12/32`, tag `allow-tunnel` — SFTP band), `gke-unity-8c75014d-*` (cluster rules).

**GCS buckets:** `unity-tunnel-config`(+`-staging`) ⚠️ (no unity equiv), `unity-youtube-extraction` ⚠️, and duplicated pairs `{unity,unity}-call-recordings`, `{unity,unity}-image-hash`, `{unity,unity}-pipeline-artifacts(-staging)`, `{unity,unity}-pod-logs` ⚠️ (unity copies created, code partly still reads unity).

**Artifact Registry:** `unity`, `unity-comms-app-repo`, `unity-adapters-repo`, `link-tracker-repo` + legacy `unity`, `unity-adapters-repo`, `unity-comms-app-repo`, `unity-livekit-app-repo`.

**Service accounts:** `comm-sa@` (all assistant pods + cluster services run as this), `unity-pipeline-worker@` + legacy `unity-pipeline-worker@`, `link-tracker-sa@`, `external-secrets-reader@`. `comm-sa` IAM is hand-managed; required roles include `roles/monitoring.metricWriter`, `roles/logging.logWriter`, `roles/pubsub.editor`, and `roles/compute.admin` + `roles/secretmanager.admin` in `gcp-project-vms`, plus `roles/storage.objectAdmin` on `gs://bucket` (cross-project; unhire archive delete).

**`comm-sa` Workspace domain-wide delegation** (Google Admin → Security → API controls → Domain-wide delegation; client ID `115203909972265828958`): authorize exactly these OAuth scopes (comma-separated when editing the client):

- `https://www.googleapis.com/auth/admin.directory.user`
- `https://www.googleapis.com/auth/gmail.send`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.settings.basic` — Gmail send-as display names for platform twin mailboxes (`deploy/scripts/set_workspace_twin_gmail_identities.py`)

After adding a scope, wait a few minutes for propagation, then run `python3 deploy/scripts/set_workspace_twin_gmail_identities.py` to sync T-W1N names on `twin@` / `staging-twin@` / `local-twin@`.

**Pub/Sub:** infra topics `unity-ingest`, `unity-parse`, `unity-dead-letter` (+`-staging`); per-assistant `unity-<id>[-staging]` (new) and `unity-<id>[-staging]` (legacy) ⚠️. ~1,590 topics / ~6,222 subs total, ~84% still `unity-*`.

**Secret Manager (names):** `UNITY_ADAPTERS_URL{,_PREVIEW,_STAGING}`, `UNITY_COMMS_URL{,_PREVIEW,_STAGING}` ⚠️, `UNITY_ADAPTERS_URL_{PRODUCTION,STAGING}`, plus provider/integration secrets (`ANTHROPIC_API_KEY`, `LIVEKIT_*`, `TWILIO_*`, `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, `VM_WILDCARD_FULLCHAIN/PRIVKEY`, `gcp-sa-key`, `github-pat`, `DEVBOT_GITHUB_TOKEN`, …). Cluster runtime secret = `unity-secrets` (see §7).

### 4.2 `gcp-project-vms` — desktop VM pool

- **Pool VMs:** Ubuntu = `unity-pool-ubuntu-<N>[-staging]` ✅ (us-central1-f / -a); Windows = `unity-pool-windows-<N>[-staging]` ⚠️.
- **Images/families:** `unity-pool-ubuntu-vm`, `unity-pool-windows-vm` (created 2026-06-24) ✅; legacy `unity-pool-ubuntu-vm`, `unity-pool-windows-vm`, `unity-ubuntu-vm`, `unity-windows-vm` (rollback source). Code: `communication/infra/vm_config.py` → `POOL_UBUNTU_VM_IMAGE_FAMILY = "unity-pool-ubuntu-vm"`, `*_IMAGE_PROJECT = "gcp-project-vms"`.
- **Static IPs:** `unity-pool-ubuntu-ip-<N>[-staging]` ✅; all Windows + preview/staging Ubuntu IPs still `unity-pool-*-ip-*` ⚠️.
- **Buckets:** `unity-assistant-archives` ✅ (per-assistant `{id}.tar.gz` Local workspace + `{id}-desktop-profile.tar.gz` browser/GUI session state) + legacy rollback bucket. Code: `POOL_ASSISTANT_ARCHIVE_BUCKET = "unity-assistant-archives"`.
- **SA:** `pool-vm-sa@gcp-project-vms` (objectAdmin — pool VMs write/read archives on release/assign) and `comm-sa@gcp-project-runtime` (objectAdmin — Cloud Run comms app deletes archives on unhire via `DELETE /infra/vm/pool/archive/{id}`; required for both staging and prod). Pool/desktop OS user is **`unityuser`** (HOME `/Unity`) ⚠️.
- **Secrets:** `VM_WILDCARD_FULLCHAIN`, `VM_WILDCARD_PRIVKEY`, `DEVBOT_GITHUB_TOKEN`.

### 4.3 `gcp-project-saas` — Orchestra + Console + DB

- **Cloud SQL (Postgres, us-central1):** `prod-ssd-usc1` (`203.0.113.14`), `staging-ssd-usc1` (`203.0.113.15`). The old `europe-west3` `prod-ssd`/`staging-ssd` are deleted; final SQL dumps live in `gs://bucket/` and `gs://bucket/` (`decommission-final-*.sql.gz`), plus a Cloud SQL final backup of `prod-ssd`.
- **Cloud Run (us-central1):** `orchestra`(+`-staging`), `saas-web-app` (Console prod), `saas-web-app-redesign-staging` (Console staging), `landing-page`(+`-staging`).
- **Secrets delta:** `UNITY_COORDINATOR_*` and `UNITY_COORDINATOR_*` both exist (DISCORD_ID/TOKEN, EMAIL_ADDRESS, PHONE_UK/US, WHATSAPP_NUMBER × PRODUCTION/STAGING) ⚠️; `UNITY_{LIVEKIT,OPENAI,DEEPGRAM,CARTESIA}_*` and `UNITY_{ADAPTERS,COMMS}_URL*` still unity-only ⚠️.

### 4.4 `gcp-project-dns` — public DNS

- Zone `unifyai` → `unify.ai.` (93 records). `api/console/unify.ai` → `203.0.113.16` (saas `public-lb-ip`). `tunnel.unify.ai` + `*.tunnel.unify.ai` → `203.0.113.13`; `staging.tunnel…` → `136.112.29.250`.
- `*.vm.unify.ai`: Ubuntu prod hosts `unity-pool-ubuntu-N.vm.unify.ai` ✅; Windows pool + most staging/preview hosts still `unity-*.vm.unify.ai` ⚠️. Liveview URLs: `https://unity-pool-<...>.vm.unify.ai/desktop/custom.html`.

---

## 5. Hosted runtime architecture

The hosted system spans three code areas: **Orchestra** (API + DB), **unity-deploy** (this repo: comms app, adapters, infra controllers), and **unity** (the per-job container on GKE).

### External services

| Service | Purpose |
|---|---|
| Twilio | Phone calls, SMS, WhatsApp |
| Gmail API / Microsoft Graph | Email (Gmail / Outlook) + Teams |
| LiveKit | Real-time audio/video (Unify Meet) |
| Google Cloud Pub/Sub | Message routing adapters ↔ containers |
| GKE (Autopilot) | Container orchestration for Unity jobs |

### Components

- **Adapters** (`adapters/`, Cloud Run `unity-adapters`): unauthenticated webhook handlers for inbound Twilio/Gmail/Microsoft, plus `/scheduled/*` cron endpoints and `/assistant/wakeup`.
- **Comms app** (`communication/`, Cloud Run `unity-comms-app`): admin-key-protected JSON API for outbound phone/SMS/email and the **infra control plane** (`/infra/pubsub/topic`, `/infra/gke/job`, `/infra/tunnel/register`, VM/session management). The same image also runs the GKE `assistant-session-controller` / `assistant-session-pool-controller`.
- **Unity container** (GKE job, image `unity/unity`): the assistant runtime. `CommsManager` subscribes to Pub/Sub; `ConversationManager` orchestrates; `EventBroker` is the in-memory bus. Pods write fleet audit rows (`AssistantJobs/startup_events`, including `liveview_url`) via ownership-scoped `/infra/assistant-jobs/*` using their own `UNIFY_KEY` — never a shared user key or `ORCHESTRA_ADMIN_KEY` on the pod.

### Fleet audit auth (`AssistantJobs`)

`AssistantJobs` is an Orchestra **system project** (`Project.is_system`, no `user_id`). Writes and Console hosted liveview reads use `ORCHESTRA_ADMIN_KEY` as the `__system__` principal on the data plane. Live job control remains K8s labels / AssistantSession / `/infra/*`.

**Never** back fleet auth with a Workspace/Console `User` row or user API key (`shared@…`, personal keys, etc.). Account-purge scripts and staging cleanups must not invent “service users” in the `user` table for infra — if a key lives on a `User`, deleting that user CASCADE-breaks the fleet.

### Pub/Sub & container lifecycle

- Per-assistant topic `unity-{assistant_id}[-staging]` (+ `-sub`). Startup topic `unity-startup[-staging]` engages idle containers.
- Message shape: `{"thread": "<type>", "event": {…}}` with threads `startup`, `msg`, `email`, `call`, `unify_message`, `unify_meet`, `unify_message_outbound`, `assistant_update`.
- **Idle → Live:** idle containers (`agent_id is None`) subscribe to the startup topic and self-ping every 30s. On inbound, the adapter checks `AssistantJobs`, publishes a `startup` message (if not live) + the inbound to the assistant topic. The container that wins the startup subscribes to its assistant topic and marks itself live. Inactivity timeout = **7 min**; the job is retained (not deleted) for logs.

### Idle pool management

Target idle count: `max(UNITY_MIN_IDLE_JOBS, live_count // UNITY_IDLE_JOB_DEMAND_FACTOR)` (defaults 3 and 5). Three mechanisms share it:
1. **Reactive fill** — on every inbound that consumes an idle job (`/scheduled/jobs/create`).
2. **Deploy refresh** — every Cloud Build (`?refresh=true`) rotates the pool to the new image.
3. **Hourly cron** — self-heal + rotation; cleanup 10 min later trims to target.

### Job-watcher (crash-safe cleanup)

A single-replica [kopf](https://kopf.dev/) operator (`base/scripts/job-watcher/`, image `unity/job-watcher`) watches pods `app=unity`. On `Succeeded`/`Failed` it sets `running=False` in `AssistantJobs` and releases the assigned pool VM — externally, so cleanup happens even if the container crashes (OOM/node failure). The in-container `mark_job_done()` (graceful exit) and the adapters' `expire_all_stale_jobs()` (periodic sweep) call the same idempotent operations.

For the per-pod 10Gi ephemeral-storage Autopilot cap and the `/tmp` `emptyDir` strategy (HuggingFace/Docling caches, `HF_HOME`, `XDG_CACHE_HOME`), see [`deploy/guides/GKE_EPHEMERAL_STORAGE.md`](deploy/guides/GKE_EPHEMERAL_STORAGE.md).

---

## 6. Assistant desktops: VM pool, tunnel/SFTP, archives, TLS

Each live assistant can get a dedicated **desktop VM** (Ubuntu or Windows) from a warm pool in `gcp-project-vms`. **This is the subsystem most damaged by the rename** — it caused the 2026-06-24 production outage.

### Pool VMs & images

- Controllers (`assistant-session-controller`) provision pool VMs from image family `unity-pool-ubuntu-vm` / `unity-pool-windows-vm` in project `gcp-project-vms`, attaching `unity-pool-ubuntu-ip-<N>` and creating `unity-pool-ubuntu-<N>.vm.unify.ai` DNS.
- Images are built by `scripts/vm-build/build-ubuntu.sh` / `build-windows.sh` (+ packer under `communication/infra/scripts/*-vm-custom-image/`), which publish to the `unity-pool-*` families. **If the family doesn't exist in GCP, every provision 404s** (the outage — see [§9](#9-known-rename-loose-ends--gotchas)).

### File sync (home filesystem persistence)

- The runtime syncs the assistant's workspace (`~/Unity/Local`, Attachments/Outputs/functions) to the desktop VM over **rclone SFTP** (`unity/file_manager/sync/`, user `unityuser`, port 2222).
- For users tunnelling a local machine, the **tunnel control plane** (`/infra/tunnel/register` in the comms app) manages a rathole relay on `unity-tunnel-server`, storing state in `gs://bucket`. SFTP uses the raw-TCP band `61000-61999` (firewall `allow-tunnel-sftp` from `unity-gke-egress-ip`). Tunnel constants live in `common/settings.py` (`tunnel_vm_name`, `tunnel_gcs_bucket`) and `communication/infra/tunnel_config.py`.

### Per-session archive (cross-session persistence)

Two GCS blobs per assistant (bucket `POOL_ASSISTANT_ARCHIVE_BUCKET` in
`vm_config.py`, passed to each VM as the `archive-bucket` metadata key):

| Blob | Contents | Restored when |
|---|---|---|
| `{assistant_id}.tar.gz` | `/Unity/Local` (workspace files synced with the Unify pod via rclone) | Next assign if the PD is empty |
| `{assistant_id}-desktop-profile.tar.gz` | Browser + agent GUI session state in home: Magnitude `browser_states`, selective Chromium/Chrome profile files, xfce4 prefs — **not** under `Local/`, so not rcloned into the pod | Next assign whenever the blob exists |

On release the watcher archives Local then the desktop-profile, unmounts the PD, and scrubs home profile paths off the shared pool VM. On permanent unhire both blobs are deleted via `DELETE /infra/vm/pool/archive/{assistant_id}`. Disk GC treats `{id}.tar.gz` as the durable-workspace signal; a missing profile only means a cold browser login. **If the bucket doesn't exist, files don't persist between sessions** (a rename gap fixed 2026-06-24).

Used by `unity-pool-watcher.sh` / `.ps1`.

### VM TLS (wildcard)

Every VM runs Caddy terminating TLS for `/api/*` (agent-service) and `/desktop/*` (noVNC). A single `*.vm.unify.ai` wildcard cert (`VM_WILDCARD_FULLCHAIN` / `VM_WILDCARD_PRIVKEY` in `gcp-project-vms` Secret Manager) is pushed to each VM via instance metadata (avoids Let's Encrypt's 50 certs/domain/week limit). Renewed monthly by the `cert-renewal` Cloud Scheduler job → `POST /scheduled/cert-renewal` (DNS-01 in the `unifyai` zone).

---

## 7. Secrets management

There are **three independent secret planes** — rotating one does not rotate the others.

### 7.1 GCP Secret Manager → GKE (`unity-secrets`)

Source of truth for runtime API keys is **GCP Secret Manager in `gcp-project-runtime`**. The **External Secrets Operator** (namespace `external-secrets`) polls SM `latest` into the `unity-secrets` K8s Secret per namespace (`refreshInterval: 1h`) via `ClusterSecretStore` `unity-gcp-secret-manager`. Unity Job pods read keys with `secretKeyRef`.

Rotate by adding a new SM **version**, then force sync + restart pods:
```bash
kubectl annotate externalsecret unity-secrets -n staging force-sync=$(date +%s) --overwrite
# then trigger a pool refresh so new pods pick it up
```
Bootstrap, break-glass (`setup_k8s_config.py`), and ESO details: [`deploy/guides/UNITY_CLUSTER_SECRETS.md`](deploy/guides/UNITY_CLUSTER_SECRETS.md). Never `kubectl apply` hand-built secret YAML; never commit key material.

### 7.2 GitHub Actions secrets (CI)

Org `unifyai` secrets are inherited by all repos: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLONE_TOKEN`, `GCP_SERVICE_ACCOUNT_JSON`, `ORCHESTRA_ADMIN_KEY`, `UNIFY_KEY`, and ⚠️ `UNITY_ADAPTERS_URL`, `UNITY_COMMS_URL`. Org variables: `GCP_PROJECT_ID=gcp-project-saas`, `GCP_LOCATION=us-central1`, `GCP_BUCKET_*`. Per-repo additions are listed in [§8](#8-cicd-cloud-build-github-actions-branches). **CI mismatch:** workflows now read `UNITY_COMMS_URL`/`UNITY_ADAPTERS_URL` but only `UNITY_*` exist → empty at runtime (see [§9](#9-known-rename-loose-ends--gotchas)).

### 7.3 VM TLS secrets

`VM_WILDCARD_FULLCHAIN` / `VM_WILDCARD_PRIVKEY` in `gcp-project-vms` Secret Manager (see §6).

### 7.4 OpenRouter API keys (LLM spend inventory)

OpenRouter is the only route to OpenAI-family models (see `AGENTS.md`) and the largest single spend surface in the estate. These keys are not a fourth secret plane — they are credentials distributed across §7.1 and §7.2 — but they rotate on their own cadence and are listed here because nothing else maps key → secret slot → consumer.

The production OpenRouter account is **`shared@unify.ai`** ("Shared Account"), *not* `dan@unify.ai` (a separate, empty personal account — the two live in different Chrome profiles). All mint / rotate / cap / rename operations run programmatically against `https://openrouter.ai/api/v1/keys` with the management key in `OPENROUTER_MANAGEMENT_API_KEY` (`gcp-project-runtime`); no browser needed. `PATCH /api/v1/keys/{hash}` is a true partial update — omitted fields keep their values.

| Key name | Hash | Cap | Held in | Consumer |
|---|---|---|---|---|
| `unify-prod-gateway-2026-08-11` | `e09094a1` | $40,000 ⚠️ **no reset** | `OPENROUTER_API_KEY` (gcp-project-runtime) + `ORCHESTRA_OPENROUTER_API_KEY` (saas) | **Production** — assistant pods + Orchestra |
| `unify-staging-runtime` | `ea72ed20` | $4,000/mo | `OPENROUTER_API_KEY_STAGING` (gcp-project-runtime) | Staging assistant pods |
| `unify-staging-orchestra` | `fb0ea371` | $4,000/mo | `ORCHESTRA_OPENROUTER_API_KEY_STAGING` (saas) | Staging Orchestra + trigger worker |
| `unify-ci-orchestra-repo` | `4f5e1fc5` | $500/mo | GH secret `OPENROUTER_API_KEY_CI` | `unifyai/orchestra` CI |
| `unify-ci-unify-repo` | `39e621a9` | $1,000/mo | GH secret `OPENROUTER_CI_API_KEY` | `unifyai/unify` CI |
| `unify-ci-unillm-repo` | `b2d9f264` | $500/mo | GH secret `OPENROUTER_API_KEY` | `unifyai/unillm` CI |
| `unify-canary-DO-NOT-DEPLOY` | `1ee9a059` | $100/mo | `OPENROUTER_CANARY_KEY` (gcp-project-runtime) | **Nothing — tripwire.** See below |
| `unify-production-RETIRED-2026-08-11` | `cfcba7cb` | disabled | — | Was production 2026-08-10 → 08-11 |
| `Default` | `d6370f6a` | disabled | — | Compromised; disabled 2026-08-10 at $32,381.27 |

CI secret names are deliberately inconsistent across repos (`OPENROUTER_API_KEY_CI` / `OPENROUTER_CI_API_KEY` / `OPENROUTER_API_KEY`) — that is the live state, not a typo. Match by repo, not by name.

**The canary is a detection control, not a spare.** `unify-canary-DO-NOT-DEPLOY` exists only as a Secret Manager entry and is deployed to no pod or service. If it ever shows usage, someone is reading Secret Manager directly; if it stays at $0 while deployed keys burn, the exposure is pod-environment access. Never deploy it.

**To identify which key a secret currently holds** (the fastest way to answer "what is production actually using?"), hash the secret and match it against the `hash` field from `GET /api/v1/keys`:

```bash
gcloud secrets versions access latest --secret=OPENROUTER_API_KEY --project=gcp-project-runtime | tr -d '\n' | shasum -a 256
```

**⚠️ Known loose ends:**

- **The production key's $40,000 cap does not reset.** Every other capped key uses `limit_reset: monthly`; this one is a one-time ceiling, so cumulative spend will hit it and stop production inference *permanently and silently* — at baseline volumes, within weeks of its 2026-08-11 mint. Either set `limit_reset: monthly` (auto-recovering, but permits $40k **per month**) or keep the hard stop and monitor usage against it deliberately. This is a live decision, not a settled design.
- **`unifyai/orchestra` holds a second, older `OPENROUTER_API_KEY` secret** (set 2026-07-01, hours after `Default` was created) alongside its current `OPENROUTER_API_KEY_CI`. It most likely still contains the disabled `Default` key; GitHub never reveals secret values, so this can only be resolved by deleting or overwriting it. Any workflow still reading it will 401.
- **`MCP: OpenRouter MCP: Claude Code (openrouter)`** (`e104bdd3`, $5 cap, $0 used) was auto-minted by an MCP integration on 2026-08-10 and is unattributed. Harmless, but it belongs to someone — attribute it or delete it.
- Per-key caps sum to roughly $90k/month of theoretical headroom. There is no account-level cap.

---

## 8. CI/CD: Cloud Build, GitHub Actions, branches

### Cloud Build (image build + GKE deploy)

Triggers fire on branch pushes; the GitHub connection is `github-unifyai` (`gcp-project-runtime/us-central1`). **All triggers and builds live in `us-central1`** — `gcloud builds {list,describe,log,triggers list}` **must** pass `--region=us-central1`, or the `global` default returns stale/empty results (see the [region cheat-sheet in §3](#3-gcp-project-map)).

| Branch | Trigger | Image | Env |
|---|---|---|---|
| `staging` | `unity-deploy-staging` | `unity-staging` | Staging |
| `main` | `unity-deploy-production` | `unity` | Production |

Each build clones `unity` (matching branch), overlays this package, pushes to Artifact Registry, updates the GCS image hash, applies `ExternalSecret` manifests, and refreshes the GKE idle pool. Cloud Build config retains `_UNITY_REF`/`_UNITY_SHA` **fallback** substitutions and `_CLUSTER: 'unity'`. ⚠️ Some legacy triggers are still bound to `repositories/unity` / `unity-deploy` and fail at source-fetch — see [§9](#9-known-rename-loose-ends--gotchas).

### GitHub Actions (per repo)

- **All repos:** `tests.yml`, `sync-staging.yml` (fast-forward `staging` after `main`).
- **orchestra:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/orchestra`), billing/cleanup cron jobs hitting `https://api.unify.ai/v0/admin/*` (prod) and `https://internal.example.com/v0/*` (staging).
- **unity:** `tests.yml` (env `unity-testing`; clones orchestra/unify/unillm + `magnitude@main`), `llm-cache-refresh.yml` (env `unity-llm-cache-refresh`), `pages.yml`.
- **unity-deploy:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/unity-selfhost`, `unity-desktop-selfhost`; `workflow_dispatch` only), `hosted-tests.yml` (`GCP_PROJECT_ID=gcp-project-runtime`).
- **console:** `ghcr-selfhost.yml` (→ `ghcr.io/unifyai/console-selfhost`), `code-quality.yml`, `security.yml`.
- **unillm:** `pypi.yml` (publish on tag).

### Branch promotion

Land changes on `staging`, let staging deploy/validate, then promote `staging` → `main`. Never merge a feature branch directly into `main`/`master`.

---

## 9. Known rename loose ends & gotchas

> **Finalization decision (2026-06-24): stabilize, do not keep migrating.** The
> platform-wide forward migration to `unity-*` is **halted**. The goal is now
> **code ⇆ live infrastructure agreement at the current mixed state**, not "everything
> must be `unity`". GitHub repo names stay `unity`/`unity-deploy`; the immutable trio
> (project IDs + cluster, row 16) stays `unity`; the cleanly-migrated pieces (Ubuntu
> pool, archives, Cloud Run, Artifact Registry, per-assistant Pub/Sub, code identifiers)
> stay `unity`; and the parts still live on `unity` (tunnel, Windows pool) are now
> **pointed at `unity` in code on purpose** rather than chased to `unity`. Code aligned
> for this: `tunnel_config.py` (tag/IP → `unity-tunnel-server*`), `vm_config.py` +
> `vm_helpers.py` (per-OS pool prefix: Ubuntu `unity-pool-*`, Windows `unity-pool-*`;
> Windows image family/tags → `unity-*`), and `unity` CI (`tests.yml`,
> `llm-cache-refresh.yml`) read the real `UNITY_COMMS_URL` secret. The 2026-06-22
> production `assistant-session-controller` crash-loop was a **controller defect**
> (the timer reconcile ran per-session GCP VM/disk ownership scans over ~110 terminal
> sessions every interval, starving its liveness probe), **not** a name mismatch; fixed
> by skipping the timer reconcile for terminally-`Released` sessions. No mass create/
> delete/DNS/Pub-Sub churn is required.

Prioritized. `P0` = can break production, `P1` = breaks CI / partial degradation, `P2` = cleanup/clarity.

| # | Pri | Item | Detail / fix |
|---|---|---|---|
| 1 | **P0** | Tunnel fixes stranded on `staging` | `8b5097a9` (tunnel bucket/VM → `unity-*`) and `244ad103` (SFTP firewall band) are on `origin/staging` **not `origin/main`**. Prod `common/settings.py` may still point tunnel at non-existent `unity-tunnel-*`. **Merging `staging`→`main` is required** for these (but does **not** fix #2/#3 which are identical on both branches). |
| 2 | **P0** | Pool image families (RESOLVED 2026-06-24) | Code wanted `unity-pool-ubuntu-vm`; only `unity-pool-ubuntu-vm` existed → 404 on every desktop provision → `assistant-session-controller` CrashLoop. Fixed by creating `unity-pool-*` families. **Keep the families fresh:** `build-ubuntu.sh`/`build-windows.sh` publish to them. |
| 3 | **P0** | Archive bucket (RESOLVED 2026-06-24) | Code wanted `unity-assistant-archives`; only `unity-assistant-archives` existed → no cross-session file persistence. Fixed by bucket create + rsync (183 objects). |
| 4 | **P1** | CI URL name split | RESOLVED (code aligned): `unity/tests.yml` + `unity/llm-cache-refresh.yml` now read the live `secrets.UNITY_COMMS_URL` instead of the non-existent `vars.UNITY_COMMS_URL`. Org secrets stay `UNITY_*` (no new resources). Re-check `unity-deploy/hosted-tests.yml` if it references these. |
| 5 | **P1** | Orphaned GitHub environments | `unity` repo has `unity-testing` (full secret set) and `unity-llm-cache-refresh` superseded by `unity-testing` / `unity-llm-cache-refresh`. Migrate env-scoped secrets + delete the `unity-*` envs. |
| 6 | **P1** | Stale Cloud Build triggers | Triggers bound to `repositories/unity` / `unity-deploy` (e.g. `adapters-unity-deploy`, `unity-comms-app-*`) fail at source-fetch (~3-6s, no steps) even though GitHub redirects the repo. Recreate as `unity-*` triggers against `repositories/unity` / `unity-deploy`. Compat files `cloudbuild/unity-comms-app*.yaml` exist for old trigger names. |
| 7 | **P1** | Referenced-but-unconfigured secrets | `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` (console build-arg), `TWILIO_*`/`LIVEKIT_*`/`GCP_SA_KEY` (unity-deploy `hosted-tests.yml`) are not configured → empty/skip. |
| 8 | **P2** | ~~`magnitude@main`~~ | RESOLVED: magnitude default/consumption branch is now `main`; install/CI/pool scripts updated. |
| 9 | **P2** | OS user `unityuser` (HOME `/Unity`) | Across unity desktop scripts + unity-deploy packer/pool scripts + `file_manager/sync/config.py`. Renaming requires rebuilding pool images. |
| 10 | **P2** | Windows pool entirely `unity-*` | RESOLVED (code aligned): Windows pool stays `unity-*` by design. `vm_config.py`/`vm_helpers.py` now use a per-OS prefix (`pool_vm_name_prefix`): Ubuntu → `unity-pool-*`, Windows → `unity-pool-*`; Windows image family → `unity-pool-windows-vm`, tag → `unity-windows-vm`. New Windows VMs/IPs/DNS now match the live pool, so replenish no longer breaks. |
| 11 | **P2** | Pub/Sub ~84% `unity-*` | 1,334 topics / 5,198 subs legacy vs 252 / 1,021 unity; per-assistant duplication. Cut over + delete legacy. |
| 12 | **P2** | Empty `unity-*` data buckets | `unity-call-recordings`, `unity-pod-logs`, `unity-pipeline-artifacts`, `unity-image-hash` created empty; code partly still reads/writes `unity-*` (which hold the data). `unity-tunnel-config`/`unity-youtube-extraction` have no unity copy. |
| 13 | **P2** | Duplicate service accounts | `unity-pipeline-worker` + `unity-pipeline-worker` both exist. |
| 14 | **P2** | App-level `unity` string constants | `UnityTests` (orchestra context-cleanup + default test project across repos), `UnitySystemEvent` gateway envelope (unity ↔ console wire contract), `WaitingForUnity` session-state labels (unity-deploy controller), `unity-user-filesync` SSH key comment (orchestra). These are deliberate cross-references, not typos — rename only with coordinated multi-repo changes. |
| 15 | **P2** | Rollback resources to clean up | Legacy `unity-pool-*` images and `unity-assistant-archives` were kept as rollback after the 2026-06-24 fix. Delete once a real session confirms restore against `unity-assistant-archives`. |
| 16 | — | Immutable `unity` names | Project IDs `gcp-project-vms` / `gcp-project-runtime` (display "Unity LiveKit"), SA emails, GKE cluster `unity`. Accept as canonical; do **not** attempt to "fix" in code (it already targets these intentionally). |

---

## 10. The deployment overlay package

`unify_deploy/` is the enterprise overlay loaded by `unity` at runtime via a Python entry point. When `_UNITY_STARTUP_HOOK_GROUP` is set (K8s Secret in hosted deploys), `unity` calls `importlib.metadata.entry_points()` and runs this package's `startup_hook()`. Absent that env var (open-source), the mechanism is inert.

The startup hook:
1. **Resolves the assistant deployment** — deployment-matched spec with org/team/user/assistant seed layers merged in scope order, plus `.secrets.json`.
2. **Syncs seed data** — hash-based idempotent sync of contacts, guidance, knowledge, secrets, blacklist to the Unify backend.
3. **Syncs custom functions** — upserts client memoized functions + venvs via `FunctionManager.sync_custom()`.

```
unify_deploy/
├── hook.py                       # entry point: startup_hook()
└── assistant_deployments/
    ├── clients/                  # client_alpha/, clientzeta/, client_beta/, …  (self-register)
    ├── configs/types/            # ActorConfig
    ├── environments/             # serialized environment reconstruction
    ├── seed_sync.py              # generic hash-based seed sync
    └── secrets_file.py           # .secrets.json parser
```

**Adding a client:** create `clients/<name>/`, define `deployments/<name>/` (each exposes a `DeploymentSpec` via `get_deployment()`), build an `EnvironmentConfig`/`DeploymentMapping` and call `register_client()` in `__init__.py`, import the client at the bottom of `clients/__init__.py`, and add any runtime secrets to `.secrets.json` (gitignored).

> **`base/` migration note:** `base/` is the private mirror of hosted base-image assets that still live in `unity/deploy/` today. Treat `base/` as the canonical private copy while live triggers run from `unity`; cut over only after the private path is verified end-to-end.

---

## 11. Local development & self-host

Setup:
```bash
git clone git@github.com:unifyai/unify.git
git clone git@github.com:unifyai/unillm.git
git clone git@github.com:unifyai/unity-deploy.git
cd unity-deploy && uv sync --all-groups && pre-commit install
```
First-party deps resolve from sibling editable checkouts. Run `pre-commit install` in every fresh checkout/worktree.

The full all-repo local stack (local Orchestra + Console + Coordinator + gateway) is in `selfhost/` — start with `bash selfhost/stack.sh up --durable`, inspect with `selfhost/stack.sh status`, reset with `selfhost/stack.sh reset`. The inner-loop runbook is [`docs/local-full-stack-inner-loop.md`](docs/local-full-stack-inner-loop.md). LiveKit is an external Cloud BYOK dependency for both source and compose stacks.

---

## 12. Troubleshooting

| Symptom | Likely cause | Debug |
|---|---|---|
| Assistant not responding | No idle container | GKE for idle jobs; `/scheduled/jobs/create` logs |
| Delayed response (minutes) | Startup queued, no idle container | Check idle jobs; trigger job creation |
| **Files vanish between sessions / "workspace was wiped"** | Desktop VM not provisioned (image family 404) or archive bucket missing | `kubectl logs deploy/assistant-session-controller -n production` for `image … was not found`; confirm `unity-pool-ubuntu-vm` family + `unity-assistant-archives` bucket exist |
| `assistant-session-controller` CrashLoop (Exit 137) | Provision loop stalls health probe (usually image-family 404) | Same as above; verify pool VMs are `RUNNING` in `gcp-project-vms` |
| `/infra/tunnel/register` 500 | Tunnel bucket name mismatch (`unity-tunnel-config` doesn't exist) | Ensure code points at `unity-tunnel-config` (loose end #1) |
| `/infra/tunnel/register` 401 | Orchestra `/user/basic-info` rejecting the key | Auth issue in tunnel control plane (separate from #1) |
| Email/SMS not received | Watch expired / contact not validated | `/scheduled/email-watches` logs; verify saved contact |
| `TLSV1_ALERT_INTERNAL_ERROR` on desktop | VM missing wildcard cert | `VM_WILDCARD_FULLCHAIN` in `gcp-project-vms`; `crt.sh/?q=%.vm.unify.ai` |
| CI deploy reads empty COMMS/ADAPTERS URL | Secret name split (#4) | Org has `UNITY_*`; workflow reads `UNITY_*` |
| Cloud Build fails instantly, no steps | Stale trigger on `repositories/unity[-deploy]` (#6) | Recreate trigger against `unity`/`unity-deploy` |
| Hiring 500 / "Rate Limit Exceeded" | GCE `instances.insert` rate limit | `gcloud logging read` in the VM project for 403s |
| Bundle publish landed but live brain_operator still on old code | Never-idle session / overlapping offline Jobs | Arm graceful drain: [`ASSISTANT_DRAIN_RESTART.md`](deploy/guides/ASSISTANT_DRAIN_RESTART.md); check ConfigMap `unity-assistant-drain-intents` |

---

## 13. Deep-dive guides

| Guide | Topic |
|---|---|
| [`deploy/guides/ASSISTANT_DRAIN_RESTART.md`](deploy/guides/ASSISTANT_DRAIN_RESTART.md) | Graceful/force assistant drain, admission close, publish fan-out |
| [`deploy/guides/UNITY_CLUSTER_SECRETS.md`](deploy/guides/UNITY_CLUSTER_SECRETS.md) | ESO bootstrap, `unity-secrets` rotation, break-glass |
| [`deploy/guides/GKE_EPHEMERAL_STORAGE.md`](deploy/guides/GKE_EPHEMERAL_STORAGE.md) | 10Gi Autopilot cap, `emptyDir` `/tmp`, HF/Docling caches |
| [`deploy/guides/TELEMETRY.md`](deploy/guides/TELEMETRY.md) | Prometheus / Cloud Monitoring metrics pipeline |
| [`deploy/guides/CALL_RECORDING.md`](deploy/guides/CALL_RECORDING.md) | Call recording storage + flow |
| [`deploy/communication/guides/DEPLOY_CHECKLIST.md`](deploy/communication/guides/DEPLOY_CHECKLIST.md) | Comms deploy checklist |
| [`docs/coordinator-onboarding-contract.md`](docs/coordinator-onboarding-contract.md) | Coordinator onboarding contract |
| [`guides/LOCAL_ASSISTANTS.md`](guides/LOCAL_ASSISTANTS.md) | Local assistant deployments |

> Maintenance: when infrastructure changes, update **this README first**. It supersedes the former `deploy/guides/INFRA.md` (folded in 2026-06). Keep the deep-dive guides for operational procedure; keep architecture + canonical names here.
