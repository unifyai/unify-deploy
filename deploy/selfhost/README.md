# Self-Host Docker Compose (internal)

Internal full-local stack using prebuilt images and Docker Compose. This is an
**internal-only** path: it brings up local Orchestra + Console + Coordinator +
gateway and depends on the private `console`/`orchestra` images. The
open-source `droid` public path instead runs the agent runtime locally against
the hosted Orchestra backend (see `droid/README.md`).

## Quick start

Run from a `droid-deploy` checkout:

```bash
bash selfhost/install-compose.sh
```

Requires **Docker** only. The installer:

1. Writes `~/.droid/docker-compose.yml` and `.env`
2. Runs the BYOK wizard (LLM + voice keys)
3. Pulls GHCR images and starts the stack
4. Opens Console at http://127.0.0.1:3000

Register on `/login`, then chat with your Coordinator.

**What you get out of the box:** Marty can chat, call, and use a **managed Linux desktop** inside Docker (view it from Console during a call via assistant screen share — served at `http://127.0.0.1:8090`).

**Control your physical machine (optional):** To let Marty drive **your real desktop** (apps and files on the host, not the Docker sandbox), install Unify Desktop Assistant on that machine. See [Control your Mac](#control-your-mac-macos), [Control your Linux desktop](#control-your-linux-desktop), or [Control your Windows desktop](#control-your-windows-desktop) below.

## Control your Mac (macOS)

Use this when Marty should drive **your physical Mac** (Finder, Chrome, logged-in apps). Skip it if the managed Docker desktop is enough.

### 1. Finish the compose install first

Complete [Quick start](#quick-start) above: `droid stack up`, sign in at Console, hire or open your Coordinator.

Copy **your API key** from Console: assistant row → **⋯** → **Connect your desktop** → **Copy API Key**. This is your Orchestra user key (the same one Marty uses), not `ORCHESTRA_ADMIN_KEY`. Paste it into the Desktop Assistant installer or tray **Settings…** when prompted. It is **not** written to `~/.droid/.env` (that file is only for stack secrets and BYOK provider keys).

### 2. Install Unify Desktop Assistant

Download the latest **`unify-desktop-assistant_*_macos.pkg`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and run the installer.

Full tray-app details: [unify-desktop-assistant/macos/README.md](https://github.com/unifyai/unify-desktop-assistant/blob/staging/macos/README.md).

**Developer alternative** (no `.pkg`): clone [unify-desktop-assistant](https://github.com/unifyai/unify-desktop-assistant) and run:

```bash
cd unify-desktop-assistant/macos/tools
./setup.sh --self-host --unify-key YOUR_KEY --link-coordinator
```

`YOUR_KEY` is the same **Copy API Key** value from Console (**Connect your desktop**), not `ORCHESTRA_ADMIN_KEY` from `~/.droid/.env`.

### 3. Enable Screen Sharing

The Desktop Assistant installer (or `setup.sh`) turns on **Screen Sharing** (Remote Management) and starts background services via launchd:

| Service | Port | Role |
|---------|------|------|
| Apple Screen Sharing (VNC) | 5900 | Your Mac's display |
| websockify (noVNC) | 6080 | Local proxy for the agent |
| agent-service | 13000 | Droid control API (Console keeps **3000**) |

On first run, macOS may prompt for **Screen Sharing** / **Accessibility** permissions — approve them.

If services show red in the menu-bar app, open **Unify Desktop Assistant → Settings…**, paste your API key again (compose self-host is detected automatically), or run **Start Services**.

Verify locally (optional): tray app → open desktop viewer, or check the menu-bar status is green.

### 4. Register and link in Console

With `droid stack up` running, open **Unify Desktop Assistant → Settings…** and paste your API key (from **Connect your desktop** in Console) if you did not enter it during install. Compose self-host is auto-detected (`~/.droid/docker-compose.yml`); setup registers `http://host.docker.internal:13000` and links the Coordinator.

Then in Console → assistant **⋯** → **Connect your desktop**:

- Confirm your Mac appears in the list and link it to the Coordinator if needed.
- **Save User Password** (macOS login password) if prompted — used later for unlock/accessibility.

### 5. Restart Droid so CM picks up the link

```bash
droid restart
```

Ask Marty to do something on your Mac (e.g. “take a screenshot of my desktop”). You do **not** need to open `http://127.0.0.1:6080/vnc.html` — that URL is for local debugging; Console shows Marty’s **managed** desktop at `:8090`, not your Mac’s noVNC feed.

## Control your Linux desktop

Use this when Marty should drive **your physical Linux session** (your logged-in desktop, not the Docker sandbox at `:8090`). Requires a graphical desktop (X11) and `droid stack up` running first.

### 1. Finish the compose install first

Same as [Quick start](#quick-start): `droid stack up`, sign in at Console, open your Coordinator.

Copy **your API key** from Console → assistant **⋯** → **Connect your desktop** → **Copy API Key** (your Orchestra user key, not `ORCHESTRA_ADMIN_KEY`).

### 2. Install Unify Desktop Assistant

Download **`unify-desktop-assistant-staging.deb`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and install:

```bash
sudo apt install ./unify-desktop-assistant-staging.deb
```

Enter your API key when prompted. If `~/.droid/docker-compose.yml` exists, the installer auto-detects compose self-host: agent listens on **13000** (Console stays on **3000**).

**Developer alternative** (no `.deb`):

```bash
cd unify-desktop-assistant/ubuntu/tools
./setup.sh --self-host --unify-key YOUR_KEY --link-coordinator
```

### 3. Background services

The assistant starts **x11vnc** (5900), **websockify/noVNC** (6080), and **agent-service** (13000 in self-host mode). The tray icon should turn green when all are up.

If registration failed because Orchestra was not up yet, run **`droid stack up`**, then tray **Settings…** → paste your API key again (or `setup.sh --reconfigure --unify-key YOUR_KEY`).

### 4. Link in Console and restart Droid

Console → **Connect your desktop** → link your machine → then:

```bash
droid restart
```

Ask Marty to do something on **your Linux desktop** (e.g. “take a screenshot of my desktop”).

## Control your Windows desktop

Use this when Marty should drive **your physical Windows desktop** (not the Docker sandbox). Requires **`droid stack up`** running (Docker Desktop) before or during assistant setup.

### 1. Finish the compose install first

Same as [Quick start](#quick-start). Copy **your API key** from Console → **Connect your desktop** → **Copy API Key**.

### 2. Install Unify Desktop Assistant

Download **`unify-desktop-assistant-staging.exe`** from [GitHub Releases](https://github.com/unifyai/unify-desktop-assistant/releases) and run the installer.

When `%USERPROFILE%\.droid\docker-compose.yml` exists, the installer detects compose self-host: **TightVNC** + **websockify** + **agent-service** on port **13000** (Console keeps **3000**).

**Developer alternative**:

```powershell
cd unify-desktop-assistant\windows\tools
.\setup.ps1 -UnifyKey YOUR_KEY -SelfHost -LinkCoordinator
```

### 3. Start stack before registration (if needed)

If install logged `Failed to connect to 127.0.0.1 port 8000`, start the stack and re-register:

```powershell
droid stack up
# then tray Settings → paste API key, or:
.\setup.ps1 -Reconfigure -UnifyKey YOUR_KEY
```

### 4. Link in Console and restart Droid

Console → **Connect your desktop** → link your PC → then:

```bash
droid restart
```

Ask Marty to do something on **your Windows desktop**.

A copy of this guide is written to `~/.droid/README.md` when you run the installer.

## Daily commands

| Command | Effect |
|---------|--------|
| `droid` (or `droid up`) | Start or resume the stack |
| `droid down` | Stop Console UI; CM + scheduler keep running |
| `droid down --full` | Stop all services |
| `droid restart` | Recreate containers after editing `.env` |
| `droid status` | Show container status |
| `droid smoke` | Verify the running local stack end-to-end |
| `droid logs [service...]` | Follow logs (optionally for specific services) |
| `droid pull` | Pull the latest images |
| `droid doctor` | Docker + key + service health |
| `droid integrations-sync` | Rerun the Builtins integrations artifact seed (needs `COMPOSIO_API_KEY`) |

Every command is also available under `droid stack <command>` (e.g. `droid stack logs`).

## Configuration

The installer generates local secrets (`POSTGRES_PASSWORD`, `ORCHESTRA_ADMIN_KEY`,
`NEXTAUTH_SECRET`, `JWT_SECRET`) and runs the BYOK wizard for provider keys.

| Key | Required for |
|-----|----------------|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY` | Coordinator chat (wizard: pick one) |
| `OPENAI_API_KEY` | Tool-search embeddings (recommended even with other chat providers) |
| `DEEPGRAM_API_KEY` + `CARTESIA_API_KEY` or `ELEVEN_API_KEY` | Browser voice calls (STT + TTS) |
| `COMPOSIO_API_KEY` | Optional provider-backed integration catalog sync |
| `UNIFY_MODEL` | Optional override; Droid picks a default when unset |

On first `docker compose up`, the one-shot `orchestra-seed` service inserts billing
plan rows Postgres needs before registration. Always start with `droid stack up`
(full stack) — starting individual services manually can skip that seed step.

The stack also seeds Builtins artifacts through the `droid-builtins-seed` one-shot
service. Core artifacts (functions and guidance) are seeded from the Droid image.
If `COMPOSIO_API_KEY` is set, the self-host integration manifest at
`deploy/selfhost/integration-bootstrap.selfhost.toml` seeds provider-backed
integration apps/tools into Orchestra's `Builtins/Integrations/*` contexts. Rerun
that artifact seed after changing provider keys or manifests:

```bash
droid integrations-sync
```

Edit `~/.droid/.env` for BYOK keys and secrets. After changes:

```bash
droid restart
```

Workspace files live at `~/Droid/Local` (bind-mounted into CM and desktop containers).

## Developer source install

Run the full stack from sibling source checkouts (for internal development).
Lay out `droid`, `unify`, `unillm`, `console`, `orchestra`, and `droid-deploy`
as siblings under one root (`UNIFY_STACK_ROOT`, defaults to the parent of
`droid-deploy`), then drive everything from this repo's `selfhost/` scripts:

```bash
bash selfhost/setup.sh        # one-time bootstrap (local Orchestra, Console env, voice)
bash selfhost/stack.sh up     # the one command to run the whole stack
```

`selfhost/stack.sh` starts Orchestra, the Droid gateway, Console (in self-host
mode), and the Coordinator runtime against the sibling checkouts.

Console's own `scripts/local.sh` is an internal dev/test harness (seeded dev
data, E2E tests) and is not the way to run the product locally — `droid stack
up` invokes it with `--self-host` for you.

## Builtins Artifacts

The compose bootstrap creates the shared `Builtins` project and seeds the core
Droid artifacts used by self-hosted assistants. Provider-backed integration
artifacts, such as Composio app/tool rows, are explicit because they require the
provider credential for the selected backend.

For development source installs, run the direct worker path from the local
`orchestra` checkout after Postgres and Orchestra migrations are available:

```bash
cd ../orchestra
poetry run python scripts/run_builtins_artifacts_seed_self_host.py \
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

## System requirements

- macOS, Linux, or Windows via WSL2
- Docker Desktop or Docker Engine
- ~12 GB RAM recommended (desktop + CM + ML deps)
- Multi-GB disk for image pulls

## LiveKit / voice (compose)

Voice uses LiveKit in `--dev` mode with ports `7880` (WS), `7881` (TCP fallback), and `7882/udp` mapped to the host. If browser calls fail on macOS Docker Desktop, confirm these ports are reachable and not blocked by a firewall. See `deploy/selfhost/LIVEKIT_COMPOSE.md` for validation steps.

## Architecture

See `deploy/selfhost/docker-compose.yml` for the full service graph: Postgres, Orchestra, Pub/Sub emulator, LiveKit, gateway, Console, CM supervisor, desktop, and Caddy proxy.

Entrypoint scripts (`cm-entrypoint.sh`, `desktop-entrypoint.sh`, `publish-desktop-ready.sh`, `ensure-pubsub-topics.sh`) ship inside the `droid-selfhost` and `droid-desktop-selfhost` images. After changing them, rebuild and publish those images — editing copies under `~/.droid/` does not affect running containers.
