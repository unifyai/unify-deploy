# Self-Host Docker Compose (internal)

Internal full-local stack using prebuilt images and Docker Compose. This is an
**internal-only** path: it brings up local Orchestra + Console + Coordinator +
gateway and depends on the private `console`/`orchestra` images. The
open-source `unity` public path instead runs the agent runtime locally against
the hosted Orchestra backend (see `unity/README.md`).

## Quick start

Run from a `unity-deploy` checkout:

```bash
bash selfhost/install-compose.sh
```

Requires **Docker** only. The installer:

1. Writes `~/.unity/docker-compose.yml` and `.env`
2. Runs the BYOK wizard (LLM + voice keys)
3. Pulls GHCR images and starts the stack
4. Opens Console at http://127.0.0.1:3000

Register on `/login`, then chat with your Coordinator.

**What you get out of the box:** Twin can chat, call, and use a **managed Linux desktop** inside Docker (view it from Console during a call via assistant screen share — served at `http://127.0.0.1:8090`).

**Control your physical machine (optional):** To let Twin drive **your real desktop** (apps and files on the host, not the Docker sandbox), install Unify Desktop Assistant on that machine. See [Control your Mac](#control-your-mac-macos), [Control your Linux desktop](#control-your-linux-desktop), or [Control your Windows desktop](#control-your-windows-desktop) below.

## Control your Mac (macOS)

Use this when Twin should drive **your physical Mac** (Finder, Chrome, logged-in apps). Skip it if the managed Docker desktop is enough.

### 1. Finish the compose install first

Complete [Quick start](#quick-start) above: `unity stack up`, sign in at Console, hire or open your Coordinator.

Copy **your API key** from Console: assistant row → **⋯** → **Connect your desktop** → **Copy API Key**. This is your Orchestra user key (the same one Twin uses), not `ORCHESTRA_ADMIN_KEY`. Paste it into the Desktop Assistant installer or tray **Settings…** when prompted. It is **not** written to `~/.unity/.env` (that file is only for stack secrets and BYOK provider keys).

### 2. Install Unify Desktop Assistant

Download the latest **`unify-desktop-assistant_*_macos.pkg`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and run the installer.

Full tray-app details: [unify-desktop-assistant/macos/README.md](https://github.com/unifyai/unify-desktop-assistant/blob/staging/macos/README.md).

**Developer alternative** (no `.pkg`): clone [unify-desktop-assistant](https://github.com/unifyai/unify-desktop-assistant) and run:

```bash
cd unify-desktop-assistant/macos/tools
./setup.sh --self-host --unify-key YOUR_KEY --link-coordinator
```

`YOUR_KEY` is the same **Copy API Key** value from Console (**Connect your desktop**), not `ORCHESTRA_ADMIN_KEY` from `~/.unity/.env`.

### 3. Enable Screen Sharing

The Desktop Assistant installer (or `setup.sh`) turns on **Screen Sharing** (Remote Management) and starts background services via launchd:

| Service | Port | Role |
|---------|------|------|
| Apple Screen Sharing (VNC) | 5900 | Your Mac's display |
| websockify (noVNC) | 6080 | Local proxy for the agent |
| agent-service | 13000 | Unity control API (Console keeps **3000**) |

On first run, macOS may prompt for **Screen Sharing** / **Accessibility** permissions — approve them.

If services show red in the menu-bar app, open **Unify Desktop Assistant → Settings…**, paste your API key again (compose self-host is detected automatically), or run **Start Services**.

Verify locally (optional): tray app → open desktop viewer, or check the menu-bar status is green.

### 4. Register and link in Console

With `unity stack up` running, open **Unify Desktop Assistant → Settings…** and paste your API key (from **Connect your desktop** in Console) if you did not enter it during install. Compose self-host is auto-detected (`~/.unity/docker-compose.yml`); setup registers `http://host.docker.internal:13000` and links the Coordinator.

Then in Console → assistant **⋯** → **Connect your desktop**:

- Confirm your Mac appears in the list and link it to the Coordinator if needed.
- **Save User Password** (macOS login password) if prompted — used later for unlock/accessibility.

### 5. Restart Unity so CM picks up the link

```bash
unity restart
```

Ask Twin to do something on your Mac (e.g. “take a screenshot of my desktop”). You do **not** need to open `http://127.0.0.1:6080/vnc.html` — that URL is for local debugging; Console shows Twin’s **managed** desktop at `:8090`, not your Mac’s noVNC feed.

## Control your Linux desktop

Use this when Twin should drive **your physical Linux session** (your logged-in desktop, not the Docker sandbox at `:8090`). Requires a graphical desktop (X11) and `unity stack up` running first.

### 1. Finish the compose install first

Same as [Quick start](#quick-start): `unity stack up`, sign in at Console, open your Coordinator.

Copy **your API key** from Console → assistant **⋯** → **Connect your desktop** → **Copy API Key** (your Orchestra user key, not `ORCHESTRA_ADMIN_KEY`).

### 2. Install Unify Desktop Assistant

Download **`unify-desktop-assistant-staging.deb`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and install:

```bash
sudo apt install ./unify-desktop-assistant-staging.deb
```

Enter your API key when prompted. If `~/.unity/docker-compose.yml` exists, the installer auto-detects compose self-host: agent listens on **13000** (Console stays on **3000**).

**Developer alternative** (no `.deb`):

```bash
cd unify-desktop-assistant/ubuntu/tools
./setup.sh --self-host --unify-key YOUR_KEY --link-coordinator
```

### 3. Background services

The assistant starts **x11vnc** (5900), **websockify/noVNC** (6080), and **agent-service** (13000 in self-host mode). The tray icon should turn green when all are up.

If registration failed because Orchestra was not up yet, run **`unity stack up`**, then tray **Settings…** → paste your API key again (or `setup.sh --reconfigure --unify-key YOUR_KEY`).

### 4. Link in Console and restart Unity

Console → **Connect your desktop** → link your machine → then:

```bash
unity restart
```

Ask Twin to do something on **your Linux desktop** (e.g. “take a screenshot of my desktop”).

## Control your Windows desktop

Use this when Twin should drive **your physical Windows desktop** (not the Docker sandbox). Requires **`unity stack up`** running (Docker Desktop) before or during assistant setup.

### 1. Finish the compose install first

Same as [Quick start](#quick-start). Copy **your API key** from Console → **Connect your desktop** → **Copy API Key**.

### 2. Install Unify Desktop Assistant

Download **`unify-desktop-assistant-staging.exe`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and run the installer.

When `%USERPROFILE%\.unity\docker-compose.yml` exists, the installer detects compose self-host: **TightVNC** + **websockify** + **agent-service** on port **13000** (Console keeps **3000**).

**Developer alternative**:

```powershell
cd unify-desktop-assistant\windows\tools
.\setup.ps1 -UnifyKey YOUR_KEY -SelfHost -LinkCoordinator
```

### 3. Start stack before registration (if needed)

If install logged `Failed to connect to 127.0.0.1 port 8000`, start the stack and re-register:

```powershell
unity stack up
# then tray Settings → paste API key, or:
.\setup.ps1 -Reconfigure -UnifyKey YOUR_KEY
```

### 4. Link in Console and restart Unity

Console → **Connect your desktop** → link your PC → then:

```bash
unity restart
```

Ask Twin to do something on **your Windows desktop**.

A copy of this guide is written to `~/.unity/README.md` when you run the installer.

## Daily commands

| Command | Effect |
|---------|--------|
| `unity` (or `unity up`) | Start or resume the stack |
| `unity down` | Stop Console UI; CM + scheduler keep running |
| `unity down --full` | Stop all services |
| `unity restart` | Recreate containers after editing `.env` |
| `unity status` | Show container status |
| `unity smoke` | Verify the running local stack end-to-end |
| `unity logs [service...]` | Follow logs (optionally for specific services) |
| `unity pull` | Pull the latest images |
| `unity doctor` | Docker + key + service health |
| `unity integrations-sync` | Rerun the Builtins integrations artifact seed (needs `COMPOSIO_API_KEY`) |

Every command is also available under `unity stack <command>` (e.g. `unity stack logs`).

## Configuration

The installer generates local secrets (`POSTGRES_PASSWORD`, `ORCHESTRA_ADMIN_KEY`,
`NEXTAUTH_SECRET`, `JWT_SECRET`) and runs the BYOK wizard for provider keys.

| Key | Required for |
|-----|----------------|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY` | Coordinator chat (wizard: pick one) |
| `OPENAI_API_KEY` | Tool-search embeddings (recommended even with other chat providers) |
| `DEEPGRAM_API_KEY` + `CARTESIA_API_KEY` or `ELEVEN_API_KEY` | Browser voice calls (STT + TTS) |
| `COMPOSIO_API_KEY` | Optional provider-backed integration catalog sync |
| `UNIFY_MODEL` | Optional override; Unity picks a default when unset |

On first `docker compose up`, the one-shot `orchestra-seed` service inserts billing
plan rows Postgres needs before registration. Always start with `unity stack up`
(full stack) — starting individual services manually can skip that seed step.

The stack also seeds Builtins artifacts through the `unity-builtins-seed` one-shot
service. Core artifacts (functions and guidance) are seeded from the Unity image.
If `COMPOSIO_API_KEY` is set, the self-host integration manifest at
`deploy/selfhost/integration-bootstrap.selfhost.toml` seeds provider-backed
integration apps/tools into Orchestra's `Builtins/Integrations/*` contexts. Rerun
that artifact seed after changing provider keys or manifests:

```bash
unity integrations-sync
```

Edit `~/.unity/.env` for BYOK keys and secrets. After changes:

```bash
unity restart
```

### Internal Coordinator communications

Shared Gmail/Twilio Coordinator identities are disabled by default. Internal
operators can opt in by placing the organization-issued credentials in:

- `~/.unity/comms_sa.json` for the Gmail service account
- `~/.unity/comms_twilio.env` for the main and WhatsApp Twilio credential pairs
  when calls are enabled; this file may stay empty for Gmail-only mode

Both files stay on the host with mode `0600` and are mounted read-only only
into services that need them. Without the calls profile, internal comms is
Gmail-only: shared Twilio polling is rejected because there is no cross-host
ownership lease. Enable Gmail polling with:

```dotenv
SELF_HOST_INTERNAL_COMMS_ENABLED=true
```

Enable the restricted Twilio call edge as well with:

```dotenv
SELF_HOST_INTERNAL_CALLS_ENABLED=true
```

The calls setting activates both Compose profiles. A pinned cloudflared quick
tunnel reaches a Caddy proxy that accepts only the exact local Twilio call,
status, and TwiML paths plus the signed LiveKit recording-complete path. The
call controller publishes the current tunnel URL to `/runtime/call-tunnel-url`,
provisions LiveKit SIP, and acquires callbacks under a persistent installation
owner ID. It requires complete main-account and WhatsApp Twilio credential
pairs. Twilio text polling starts only after both owner-tagged call callbacks
are verified, and stops if that remote lease is lost. Full shutdown restores
the captured prior callback state only when the callbacks still match this
installation; otherwise shutdown stops and reports the ownership conflict.

Workspace files live at `~/Unity/Local` (bind-mounted into CM and desktop containers).

## Developer source install

Run the full stack from sibling source checkouts (for internal development).
Lay out `unify`, `unillm`, `console`, `orchestra`, and `unity-deploy`
as siblings under one root (`UNIFY_STACK_ROOT`, defaults to the parent of
`unity-deploy`), then drive everything from this repo's `selfhost/` scripts:

```bash
bash selfhost/setup.sh        # one-time bootstrap (local Orchestra, Console env, voice)
bash selfhost/stack.sh up     # fresh redeploy from scratch, then smoke-test
```

`selfhost/stack.sh up` is intentionally scratch-first because that is the normal
developer loop. It stops any previous source stack, clears stale durable tmux
state, purges the local Orchestra database, clears stale runtime identity files
under `~/.unity`, and starts the local services. On a fresh install this leaves
the stack in a pre-signup state; create the local owner in Console, then Console
starts the single Coordinator runtime for that user.

There are two expected states:

- **Pre-signup:** infra is running, but no user or Coordinator exists yet.
- **Post-signup:** the single UI-created owner has one personal Coordinator, and
  the local runtime state files point at that same owner/Coordinator pair.

Setup never provisions a placeholder owner. If duplicate owners or Coordinators
appear, reset back to the real owner (or pre-signup if no owner exists yet).

Use `bash selfhost/stack.sh resume` only when you deliberately want to preserve
the current local chat/onboarding/project history. `bash selfhost/stack.sh down
--full` still stops everything.

If a smoke check fails, rerun the canonical redeploy:

```bash
bash selfhost/stack.sh up
```

That path repairs the common local sharp edges: stale `coordinator-runtime.json`,
a stale `unity-stack` tmux session, missing Builtins catalogue rows, or a stopped
Unity gateway.

Console's own `scripts/local.sh` is an internal dev/test harness (seeded dev
data, E2E tests) and is not the way to run the product locally — `unity stack
up` invokes it with `--self-host` for you.

### Phone & WhatsApp calls (source install)

Browser/Console voice (Unify Meet), Unity voice workers, room APIs, and real
**inbound/outbound phone and WhatsApp calls** use one LiveKit Cloud project in
the source stack. This keeps the localhost app code local while treating media
transport like the other BYOK services.

- A **public webhook**: a call is synchronous (Twilio POSTs the number's voice
  URL and needs TwiML back in seconds), so it cannot be polled like SMS/WhatsApp
  text. `stack.sh` runs a managed `cloudflared` tunnel to the local CM ingress
  and points the localhost number's `VoiceUrl` at it. Text stays poll-only.
- **LiveKit Cloud**: Console mints browser tokens for the cloud room, the local
  Unity worker connects outbound to the same project, and Twilio uses LiveKit
  Cloud SIP for the phone media leg. Only the HTTP webhook is tunneled — no
  SIP/RTP tunneling from the laptop.

Configure the LiveKit Cloud side once, then start the stack:

```bash
# 1. Create a LiveKit Cloud project (https://cloud.livekit.io), enable SIP.
# 2. Provide its creds + SIP URI (BYOK wizard, or write ~/.unity/livekit_cloud.env):
#      LIVEKIT_URL=wss://<project>.livekit.cloud
#      LIVEKIT_API_KEY=...
#      LIVEKIT_API_SECRET=...
#      LIVEKIT_SIP_URI=<project>.sip.livekit.cloud
# 3. Start the stack:
bash selfhost/stack.sh up
```

On `up` the stack: installs `cloudflared`, starts the tunnel,
ensures a LiveKit Cloud inbound SIP trunk covers the localhost numbers
(`deploy/selfhost/provision_call_sip.py`), and points the voice webhook at the tunnel
(`deploy/selfhost/sync_comms_webhooks.py --set-voice`). If any required call setup
step fails, startup stops rather than leaving a broken inbound-call path.
`stack.sh down --full` reverts the voice webhook and stops the tunnel.

For a deliberately text-only local stack, set `SELF_HOST_CALLS_ENABLED=0`; browser
Meet still uses the LiveKit Cloud media credentials.

Caveats:

- **Single-owner voice number.** A number has one `VoiceUrl`, so only one
  developer can own the shared localhost voice number's calls at a time. SMS and
  WhatsApp text stay poll-only locally, and local Orchestra resolves ownership
  before the bridge forwards anything to the local Coordinator. `down --full`
  reverts the voice webhook.
- **Losing that race is not a startup failure.** When another live install holds
  the number, `up` logs a warning and brings the rest of the stack up without
  inbound calls; only a genuinely broken call edge (missing credentials, dead
  tunnel, failed SIP trunk) stops startup. The claim has no expiry, so a plain
  `down` leaves it held — the owning install must run `down --full` to release
  it. `sync-comms --set-voice` exits `3` for this case, distinct from `2`.
- cloudflared quick tunnels get a fresh URL each run; the voice webhook is
  re-synced automatically on `up` and whenever the tunnel restarts.
- WhatsApp Business Calling additionally needs the feature enabled on the Twilio
  account.

## Provider-event triggers (opt-in)

Third-party provider triggers (Composio or Pipedream `github.issue_created`) are
disabled by default. Enable them only when you can expose a **public HTTPS**
callback to Orchestra and run the dedicated trigger worker.

### Docker Compose install

In `~/.unity/.env`:

```bash
SELF_HOST_PROVIDER_TRIGGERS_ENABLED=true
ORCHESTRA_TRIGGER_CALLBACK_BASE_URL=https://triggers.example.com
TRIGGER_EVENT_WRAPPING_MASTER_KEY=<long-random-secret>
```

Configure credentials for the backends you use:

```bash
# Composio triggers (live provision + project webhook signing)
COMPOSIO_API_KEY=<composio-api-key>
COMPOSIO_WEBHOOK_SECRET=<composio-project-webhook-secret>

# Pipedream triggers (live Connect provision; signing keys are per-trigger)
PIPEDREAM_CLIENT_ID=<pipedream-client-id>
PIPEDREAM_CLIENT_SECRET=<pipedream-client-secret>
PIPEDREAM_PROJECT_ID=<pipedream-project-id>
PIPEDREAM_ENVIRONMENT=development
```

Pipedream-only self-host can omit `COMPOSIO_*` and use the local stub when
Connect credentials are unset. Composio-only stacks still need
`COMPOSIO_WEBHOOK_SECRET` for ingress verification. With provider triggers
enabled, the Pipedream integration backend is enabled automatically for
connection flows even without live Connect credentials.

Then restart the stack:

```bash
unity stack up
```

What the `provider-triggers` profile adds:

- `trigger-ingress` (Caddy) exposes **only**
  `POST /v0/webhooks/integrations/*` and returns 404 for every other path.
  Local test URL: `http://127.0.0.1:8088`.
- `orchestra-trigger-worker` runs subscription reconciliation, dispatch
  delivery, and private blob maintenance.
- `provider-event-blobs` volume stores encrypted matched event payloads separately
  from the public `local-bucket` media namespace.

Point your own HTTPS reverse proxy (or TLS-terminated load balancer) at
`trigger-ingress:8080` on the Docker network, or publish that service on a host
port. Unify does **not** provide a managed relay, Cloudflare tunnel, or automatic
tunnel for provider callbacks, and paused or unhealthy intervals have no event
backfill guarantee.

### Source install (`stack.sh`)

Use the same variables in `unity/.env` or `~/.unity/.env` (the latter overlays
the former when both exist):

```bash
SELF_HOST_PROVIDER_TRIGGERS_ENABLED=true
ORCHESTRA_TRIGGER_CALLBACK_BASE_URL=https://triggers.example.com
TRIGGER_EVENT_WRAPPING_MASTER_KEY=<long-random-secret>
# Optional per backend — see Docker Compose section above
COMPOSIO_API_KEY=<composio-api-key>
COMPOSIO_WEBHOOK_SECRET=<composio-project-webhook-secret>
PIPEDREAM_CLIENT_ID=<pipedream-client-id>
PIPEDREAM_CLIENT_SECRET=<pipedream-client-secret>
PIPEDREAM_PROJECT_ID=<pipedream-project-id>
```

Then start or restart the source stack:

```bash
bash selfhost/stack.sh up
```

The stack starts a local `orchestra.workers.provider_trigger_worker` process
(readiness on port `8081` by default) and stores private event blobs under
`~/.unity/provider-event-blobs`. Point your HTTPS reverse proxy at Orchestra's
webhook route on the host:

`http://127.0.0.1:8000/v0/webhooks/integrations/*`

Unify does not run a managed callback tunnel for provider triggers on the source
path (unlike phone-call voice webhooks). The background runtime supervisor
keeps the worker alive while headless scheduling is enabled.

Verify prerequisites with:

```bash
unity stack doctor    # Compose install
bash selfhost/stack.sh doctor   # Source install
```

## Builtins Artifacts

The bootstrap creates a system-owned `Builtins` project and seeds the core Unity
artifacts used by self-hosted assistants before any user signs up. Provider-backed
integration artifacts, such as Composio app/tool rows, are explicit because they
require the provider credential for the selected backend.

For development source installs, run the direct worker path from the local
`orchestra` checkout after Postgres and Orchestra migrations are available:

```bash
cd ../orchestra
uv run python scripts/run_builtins_artifacts_seed_self_host.py \
  --manifest deploy/integrations/bootstrap.selfhost.toml \
  --backend-id composio \
  --workers 4 \
  --batch-size 25 \
  --write-request-file /tmp/builtins-artifacts-request.json
```

This path has no Cloud Run, GCS, `gcloud`, Cloud SQL connector, or Secret
Manager dependency. It writes `IntegrationBootstrapState` plus Builtins
`Integrations/Meta` checkpoint rows, so rerunning the same request resumes from
completed batches instead of falling back to the inline API path.

### Catalogue reuse across redeploys

A full provider catalogue is tens of thousands of rows and takes ~30 minutes to
sync. Orchestra skips the sync when `IntegrationBootstrapState`'s `desired_hash`
still matches, but `up` deletes the Postgres volume, so that state — and the
catalogue it guards — is gone on every fresh redeploy.

`up` therefore snapshots the `Builtins` project to
`~/.unity/builtins-catalog/<alembic-head>-<manifest-sha>.sql.gz` after a
successful seed, and restores it into the new database before the next seed
runs. The hash comparison then short-circuits normally. A schema or manifest
change produces a different key and simply misses, falling through to a full
sync; developer data is never captured, so `up` still purges it.

The catalogue is provider metadata rather than anything install-specific, so one
machine's snapshot seeds another. Point `UNITY_BUILTINS_CATALOG_URL` at an https
location serving `<key>.sql.gz` and a first `up` on a new machine downloads the
catalogue instead of syncing it.

> A snapshot is SQL executed against the local database. Only point
> `UNITY_BUILTINS_CATALOG_URL` at a location you control. Downloads are rejected
> unless they contain nothing but `COPY` data blocks, which bounds a corrupt or
> tampered file but does not replace trusting the host.

## System requirements

- macOS, Linux, or Windows via WSL2
- Docker Desktop or Docker Engine
- ~12 GB RAM recommended (desktop + CM + ML deps)
- Multi-GB disk for image pulls

## LiveKit / voice

Compose and source installs both use LiveKit Cloud for browser Meet, Unity voice
workers, room APIs, and SIP. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and
`LIVEKIT_API_SECRET` in the compose `.env`; set `LIVEKIT_SIP_URI` when phone or
WhatsApp calls are enabled. The bundle does not ship or start a local
media server.

## Architecture

See `deploy/selfhost/docker-compose.yml` for the full service graph: Postgres, Orchestra, Pub/Sub emulator, gateway, Console, CM supervisor, desktop, and Caddy proxy.

Entrypoint scripts (`cm-entrypoint.sh`, `desktop-entrypoint.sh`, `publish-desktop-ready.sh`, `ensure-pubsub-topics.sh`) ship inside the `unity-selfhost` and `unity-desktop-selfhost` images. After changing them, rebuild and publish those images — editing copies under `~/.unity/` does not affect running containers.
