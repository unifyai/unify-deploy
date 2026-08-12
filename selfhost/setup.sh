#!/usr/bin/env bash
# ============================================================================
# Unity setup — local backend bootstrap
# ============================================================================
# Spins up a local orchestra instance (Postgres+pgvector in Docker +
# FastAPI server) and wires Unity's .env to use it. Idempotent: safe to
# re-run. Sibling repos (including orchestra) are cloned by install.sh;
# setup syncs orchestra and installs runtime dependencies.
#
# Usually called automatically by scripts/install.sh; re-run directly via
# `unity setup` if you need to re-bootstrap (e.g., Docker wasn't running the
# first time, or you wiped ~/.unity).
#
# Options:
#   --boot-runtime   Install a login boot hook so background scheduling survives reboot
#
# Environment (all optional):
#   UNITY_HOME              Install root (default: ~/.unity)
#   ORCHESTRA_PORT          Orchestra FastAPI port (default: 8000)
#   ORCHESTRA_DB_PORT       Postgres port (default: 55432)
#   UNITY_SKIP_ORCHESTRA    If "1", skip the orchestra spin-up (env only)
# ============================================================================

set -e

# --- Config ---------------------------------------------------------------
# Resolve repo locations from this script's own location so `unity setup`
# operates on the checkout it ships with (and the sibling repos beside it) —
# whether that's the installer's ~/.unity tree or a developer's own clones
# (e.g. ~/dev/{unity,console,orchestra,...}). This mirrors stack.sh's
# UNIFY_STACK_ROOT resolution so setup and stack always target the same repos.
# Explicit UNITY_HOME / UNIFY_STACK_ROOT / *_REPO env vars still win.
# This script lives in unity-deploy/selfhost/ and bootstraps the sibling unify,
# console, and orchestra checkouts located under UNIFY_STACK_ROOT (defaults to
# the parent of unity-deploy). Explicit UNITY_HOME / UNIFY_STACK_ROOT / *_REPO
# env vars still win.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=default_repo_paths.sh
source "$SCRIPT_DIR/default_repo_paths.sh"
DEPLOY_REPO="$(cd "$SCRIPT_DIR/.." && pwd -P)"
UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO/.." && pwd -P)}"
UNITY_HOME="${UNITY_HOME:-$UNIFY_STACK_ROOT}"
UNITY_REPO="${UNITY_REPO:-${UNITY_REPO_PATH:-$(default_unity_repo_path "$UNIFY_STACK_ROOT")}}"
export UNITY_REPO_PATH="${UNITY_REPO_PATH:-$UNITY_REPO}"
ORCHESTRA_REPO="${ORCHESTRA_REPO:-${UNITY_HOME}/orchestra}"
CONSOLE_REPO="${CONSOLE_REPO:-${UNITY_HOME}/console}"
ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}"
CONSOLE_PORT="${CONSOLE_PORT:-3000}"
UNITY_BRANCH="${UNITY_BRANCH:-staging}"

# Ensure user-local tool dirs are on PATH. `uv` lands here, and in a fresh
# shell it may not be picked up.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# --- Colors / logging -----------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

has_env_value() {
    local key="$1"
    [[ -f "$UNITY_REPO/.env" ]] && grep -qE "^${key}=.+$" "$UNITY_REPO/.env"
}

default_orchestra_db_port() {
    if command -v docker >/dev/null 2>&1; then
        local mapped
        mapped="$(docker port orchestra-local-db 5432/tcp 2>/dev/null | head -1 | sed 's/.*://')"
        if [[ -z "$mapped" ]]; then
            mapped="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' orchestra-local-db 2>/dev/null || true)"
        fi
        if [[ -n "$mapped" ]]; then
            printf '%s' "$mapped"
            return 0
        fi
    fi
    if [[ -n "${ORCHESTRA_DB_PORT:-}" ]]; then
        printf '%s' "$ORCHESTRA_DB_PORT"
        return 0
    fi
    printf '55432'
}

ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"

log_stage() {
    echo ""
    echo -e "${BOLD}$1${NC}"
}

_load_install_progress() {
    if [[ -f "$SCRIPT_DIR/install_progress.sh" ]]; then
        # Set here rather than changing the shared default, which other installers
        # size for their own step count.
        export INSTALL_PROGRESS_TOTAL="${INSTALL_PROGRESS_TOTAL:-8}"
        # shellcheck disable=SC1091
        source "$SCRIPT_DIR/install_progress.sh"
        return 0
    fi
    progress_step_begin() { log_info "$2"; }
    progress_step_update() { :; }
    progress_step_end_success() { log_success "Done: $2"; }
    progress_step_end_fail() { log_error "Failed: $2"; return 1; }
    progress_step_run() { local _s="$1" _l="$2"; shift 2; log_info "$_l"; "$@"; }
    progress_repo_line() { log_success "[$1] $2"; }
    progress_repo_fail() { log_error "[$1] $2"; }
}

SHALLOW_CLONE_DEPTH="${SHALLOW_CLONE_DEPTH:-1}"

_sync_orchestra_repo() {
    if git -C "$ORCHESTRA_REPO" rev-parse --is-shallow-repository 2>/dev/null | grep -q true; then
        git -C "$ORCHESTRA_REPO" fetch --depth "$SHALLOW_CLONE_DEPTH" origin "$UNITY_BRANCH" || return 1
    else
        git -C "$ORCHESTRA_REPO" fetch origin "$UNITY_BRANCH" || return 1
    fi
    git -C "$ORCHESTRA_REPO" checkout "$UNITY_BRANCH" 2>/dev/null || {
        log_warn "[orchestra] Couldn't checkout $UNITY_BRANCH (uncommitted changes?). Leaving as-is."
        return 0
    }
    if ! git -C "$ORCHESTRA_REPO" reset --hard "origin/$UNITY_BRANCH" 2>/dev/null; then
        git -C "$ORCHESTRA_REPO" pull --ff-only origin "$UNITY_BRANCH" 2>/dev/null || {
            log_warn "[orchestra] Fast-forward pull skipped; leaving as-is."
            return 0
        }
    fi
    return 0
}

# --- Docker ---------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Linux*) echo "linux" ;;
        Darwin*) echo "macos" ;;
        *) echo "unknown" ;;
    esac
}

install_docker_interactive() {
    local os; os="$(detect_os)"
    log_warn "Docker is not installed (required for local orchestra)."
    case "$os" in
        macos)
            echo "  On macOS, install Docker Desktop:"
            echo "    • via Homebrew:  brew install --cask docker"
            echo "    • or download:   https://www.docker.com/products/docker-desktop"
            if [ -t 0 ] && command -v brew >/dev/null 2>&1; then
                read -r -p "  Install via Homebrew now? [y/N] " ans
                if [[ "$ans" =~ ^[Yy]$ ]]; then
                    brew install --cask docker
                    log_info "After Docker Desktop opens and finishes initial setup, re-run: unity setup"
                fi
            fi
            ;;
        linux)
            echo "  On Linux, install Docker Engine:"
            echo "    • Debian/Ubuntu:  curl -fsSL https://get.docker.com | sh"
            echo "    • Then:           sudo usermod -aG docker \$USER  (and log out/in)"
            if [ -t 0 ]; then
                read -r -p "  Run the official Docker install script now? [y/N] " ans
                if [[ "$ans" =~ ^[Yy]$ ]]; then
                    curl -fsSL https://get.docker.com | sh
                    log_info "After Docker is configured (sudo usermod -aG docker \$USER), re-run: unity setup"
                fi
            fi
            ;;
        *)
            echo "  Your OS isn't auto-detected. See https://docs.docker.com/get-docker/"
            ;;
    esac
    return 1
}

ensure_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        install_docker_interactive
        return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        log_warn "Docker is installed but the daemon isn't running."
        if [ "$(detect_os)" = "macos" ] && [ -d /Applications/Docker.app ]; then
            log_info "Starting Docker Desktop..."
            open -a Docker
            local i
            for i in $(seq 1 60); do
                if docker info >/dev/null 2>&1; then break; fi
                sleep 1
            done
            if ! docker info >/dev/null 2>&1; then
                log_error "Docker didn't become ready within 60s. Please start it and re-run: unity setup"
                return 1
            fi
        else
            log_error "Start your Docker daemon and re-run: unity setup"
            return 1
        fi
    fi
    log_success "Docker: $(docker --version 2>/dev/null | head -1)"
}

# --- uv (for orchestra) -----------------------------------------------------
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        log_success "uv: $(uv --version 2>/dev/null)"
        return 0
    fi
    log_error "uv not on PATH. scripts/install.sh should have installed it; re-run install.sh?"
    return 1
}

# --- orchestra repo (cloned by install.sh; sync here) ----------------------
ensure_orchestra_repo() {
    if [ -d "$ORCHESTRA_REPO/.git" ]; then
        _sync_orchestra_repo || return 1
    else
        log_warn "[orchestra] Missing at $ORCHESTRA_REPO — install.sh should have cloned it."
        mkdir -p "$UNITY_HOME"
        if ! git clone --quiet --depth "$SHALLOW_CLONE_DEPTH" --single-branch \
            --branch "$UNITY_BRANCH" "https://github.com/unifyai/orchestra.git" "$ORCHESTRA_REPO" 2>/dev/null; then
            return 1
        fi
    fi
    return 0
}

install_orchestra_deps() {
    log_info "Installing orchestra dependencies via uv (first run may take several minutes)..."

    # Capture install output so a failure surfaces the actual cause instead
    # of an opaque "uv sync failed" message. uv provisions a managed Python
    # 3.12 automatically when the system lacks one.
    local install_log
    install_log="$(mktemp)"
    if (cd "$ORCHESTRA_REPO" && uv sync --frozen) >"$install_log" 2>&1; then
        rm -f "$install_log"
        log_success "orchestra dependencies installed"
    else
        log_error "uv sync failed in $ORCHESTRA_REPO"
        echo ""
        echo "  --- Last 40 lines of uv output ---"
        tail -40 "$install_log" | sed 's/^/  /'
        echo "  --------------------------------------"
        echo "  Full log: $install_log"
        echo ""
        log_info "Try manually:  cd $ORCHESTRA_REPO && uv sync"
        return 1
    fi
}

# --- orchestra spin-up -----------------------------------------------------
start_local_orchestra() {
    log_info "Starting local orchestra (Docker Postgres+pgvector + FastAPI)..."

    if command -v lsof >/dev/null 2>&1 && lsof -i ":${ORCHESTRA_DB_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
        local db_container
        db_container=$(docker ps --filter "publish=${ORCHESTRA_DB_PORT}" --format "{{.Names}}" 2>/dev/null | head -1)
        if [ "$db_container" != "orchestra-local-db" ]; then
            log_error "Postgres port ${ORCHESTRA_DB_PORT} is already in use."
            log_info "Stop the process using it, or re-run with a different port:"
            log_info "  ORCHESTRA_DB_PORT=55433 unity setup"
            return 1
        fi
    fi

    # Disable auto-shutdown: local installs should stay up until `unity stop`
    export ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS=0
    export ORCHESTRA_PORT
    export ORCHESTRA_DB_PORT
    export ORCHESTRA_PREFIX="${ORCHESTRA_PREFIX:-orchestra}"
    export ORCHESTRA_REPO_PATH="$ORCHESTRA_REPO"

    # Run orchestra's local.sh; tee to terminal AND a log file so we can
    # parse the final `export UNIFY_BASE_URL=... / UNIFY_KEY=...` lines out of
    # it.
    local tmp_log
    tmp_log="$(mktemp)"
    bash "$ORCHESTRA_REPO/scripts/local.sh" start 2>&1 | tee "$tmp_log"
    local start_exit=${PIPESTATUS[0]}

    if (( start_exit != 0 )); then
        rm -f "$tmp_log"
        log_error "orchestra failed to start (local.sh exit=$start_exit). See output above."
        log_info "Common causes: port $ORCHESTRA_PORT / $ORCHESTRA_DB_PORT in use, Docker daemon not running."
        log_info "Re-run with:  unity setup"
        return 1
    fi

    # Extract the export lines from the log
    local env_block
    env_block=$(grep -E '^export (UNIFY_BASE_URL|UNIFY_KEY)=' "$tmp_log" | tail -2)
    rm -f "$tmp_log"

    if [ -z "$env_block" ]; then
        log_error "orchestra started but didn't emit UNIFY_BASE_URL / UNIFY_KEY lines."
        return 1
    fi

    # Source the block (safe: local.sh emits simple 'export KEY=value' lines)
    eval "$env_block"

    # Sanity: refuse non-local URLs. The expected output is a 127.0.0.1 /
    # localhost URL since orchestra's local.sh has no remote-fallback
    # path; anything else means a downstream change broke the contract.
    case "${UNIFY_BASE_URL:-}" in
        http://127.0.0.1:*|http://localhost:*) ;;
        *)
            log_error "orchestra emitted a non-local URL (${UNIFY_BASE_URL:-empty}). Refusing to wire."
            return 1
            ;;
    esac

    log_success "orchestra URL: ${UNIFY_BASE_URL}"
    log_success "Local UNIFY_KEY:    ${UNIFY_KEY}"
}

# --- Wire into Unity's .env -----------------------------------------------
wire_unity_env() {
    local env_file="$UNITY_REPO/.env"
    if [ ! -f "$env_file" ]; then
        if [ -f "$UNITY_REPO/.env.example" ]; then
            cp "$UNITY_REPO/.env.example" "$env_file"
            log_info "Created $env_file from .env.example"
        else
            touch "$env_file"
        fi
    fi

    # Idempotent upsert of KEY=VALUE
    upsert() {
        local key="$1" val="$2"
        if grep -qE "^${key}=" "$env_file"; then
            # Replace existing line (portable sed for macOS + linux)
            python3 - "$env_file" "$key" "$val" <<'PYEOF'
import sys, re
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    lines = f.readlines()
pat = re.compile(rf'^{re.escape(key)}=')
for i, line in enumerate(lines):
    if pat.match(line):
        lines[i] = f'{key}={val}\n'
        break
with open(path, 'w') as f:
    f.writelines(lines)
PYEOF
        else
            printf '%s=%s\n' "$key" "$val" >> "$env_file"
        fi
    }

    upsert "ORCHESTRA_URL" "$UNIFY_BASE_URL"
    upsert "UNIFY_KEY" "$UNIFY_KEY"

    log_success "Wrote ORCHESTRA_URL and UNIFY_KEY to $env_file"
}

# --- Console .env.local bootstrap -----------------------------------------
# Mint a strong random secret for auth signing / admin credentials. Prefers
# openssl, falls back to python3, then /dev/urandom — one of these exists on
# every supported macOS / Linux / WSL2 box.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr -d '\n'
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
    else
        head -c 32 /dev/urandom | base64 | tr -d '\n'
    fi
}

# Idempotently set KEY=VALUE in an env file. Replaces an existing assignment or
# appends a new one, leaving every other key untouched. Creates the file if it
# does not exist.
upsert_env_var() {
    local file="$1" key="$2" value="$3"
    touch "$file"
    if grep -qE "^${key}=" "$file"; then
        local tmp
        tmp="$(mktemp)"
        grep -vE "^${key}=" "$file" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        mv "$tmp" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

# Generate console/.env.local for self-host. Console has no database of its
# own — it uses JWT sessions + OrchestraAdapter, so all persistence flows
# through the local Orchestra. The only required config is therefore auth
# signing secrets and the local Orchestra wiring. Everything feature-related
# (billing, voice, workspace OAuth, ...) is derived at runtime from the
# credentials in unity/.env, which stack.sh propagates into the Console
# process — so this file deliberately contains no provider keys.
bootstrap_console_env() {
    if [ ! -d "$CONSOLE_REPO" ]; then
        log_warn "console repo not found at $CONSOLE_REPO — skipping Console env bootstrap."
        log_info "Re-run scripts/install.sh to clone Console (or set CONSOLE_REPO)."
        return 0
    fi

    local env_file="$CONSOLE_REPO/.env.local"
    if [ -f "$env_file" ]; then
        # Don't clobber an existing file's secrets or wiring, but ensure the
        # durable self-host topology flags are set so any local Console start
        # (including a bare `npm run dev`) resolves as a self-host deployment.
        log_info "Console .env.local present — ensuring self-host flags are set..."
        upsert_env_var "$env_file" SELF_HOST 1
        upsert_env_var "$env_file" NEXT_PUBLIC_SELF_HOST 1
        upsert_env_var "$env_file" NEXT_PUBLIC_CONSOLE_DEBUG true
        log_success "Ensured self-host flags in $env_file"
        return 0
    fi

    log_info "Generating Console .env.local (auth secrets + local Orchestra wiring)..."

    local nextauth_secret jwt_secret admin_key
    nextauth_secret="$(gen_secret)"
    jwt_secret="$(gen_secret)"
    # Console reads ORCHESTRA_ADMIN_KEY from .env.local and forwards it to the
    # local Orchestra when the stack starts, so both sides share this minted
    # credential instead of the hardcoded `local-admin-key` dev fallback.
    admin_key="$(gen_secret)"

    cat > "$env_file" <<EOF
# Auto-generated by \`unity setup\` on $(date +%Y-%m-%d).
# Self-host Console config. Secrets are minted per-install; safe to edit.
#
# This file contains ONLY auth signing secrets + local Orchestra wiring.
# Feature availability (billing, voice, transcription, workspace OAuth, ...)
# is derived from the credentials you add to $UNITY_REPO/.env — the stack
# scripts propagate those into the Console process automatically.

# NextAuth / session signing. Console uses JWT sessions + OrchestraAdapter and
# keeps no database of its own; all persistence goes through Orchestra.
NEXTAUTH_SECRET=${nextauth_secret}
JWT_SECRET=${jwt_secret}
NEXTAUTH_URL=http://localhost:${CONSOLE_PORT}

# Local Orchestra backend (started by \`unity setup\`). The stack scripts
# override these at runtime too, but they keep standalone Console runs working.
ORCHESTRA_URL=http://127.0.0.1:${ORCHESTRA_PORT}
ORCHESTRA_ADMIN_KEY=${admin_key}

# Deployment topology: a single-owner self-host install. These flags make
# Console resolve as self-host on every local start, so multi-tenant surfaces
# (Organizations, Billing) stay hidden and registration is the only way in.
# See console/src/lib/environment/environment.ts.
SELF_HOST=1
NEXT_PUBLIC_SELF_HOST=1
NEXT_PUBLIC_CONSOLE_DEBUG=true
EOF

    log_success "Wrote $env_file"
}

# --- Voice stack (LiveKit Cloud + BYOK keys) -------------------------------
setup_voice_defaults() {
    log_info "Checking LiveKit Cloud voice configuration..."

    if [[ -f "$SCRIPT_DIR/self_host_env.sh" ]]; then
        # shellcheck disable=SC1090
        source "$SCRIPT_DIR/self_host_env.sh"
        self_host_export_livekit_backend
        if self_host_livekit_media_configured; then
            log_success "LiveKit Cloud media credentials configured"
            return 0
        fi
    fi

    log_warn "LiveKit Cloud media credentials are not configured yet."
    log_info "The BYOK wizard will write them to ${SELF_HOST_LIVEKIT_CLOUD_FILE:-${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/livekit_cloud.env}"
}

ensure_console_npm_deps() {
    if [ ! -d "$CONSOLE_REPO" ]; then
        return 0
    fi
    local prereqs_script="$SCRIPT_DIR/ensure_prereqs.sh"
    if [ -f "$prereqs_script" ]; then
        # shellcheck disable=SC1090
        source "$prereqs_script"
        ensure_node || return 1
    elif ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        log_error "Node.js 20+ and npm are required for Console"
        return 1
    fi
    if [ -d "$CONSOLE_REPO/node_modules" ]; then
        log_success "Console npm dependencies already installed"
        return 0
    fi
    log_info "Installing Console npm dependencies (first run may take a few minutes)..."
    (cd "$CONSOLE_REPO" && npm install --no-fund --no-audit)
    log_success "Console npm dependencies installed"
}

# --- Canvas authoring toolchain -------------------------------------------
# Canvas is authored as real TSX, so the assistant needs a node toolchain to
# lint, typecheck and bundle it, plus the runtime host to render it before
# publishing. The hosted image bakes both in (unify/deploy/Dockerfile); source
# mode builds nothing, so without this step canvas authoring fails with
# "toolchain is unavailable" and the render gate silently skips.
#
# Installed to the paths CanvasManager probes when unconfigured, so nothing needs
# wiring into unify/.env — one source of truth rather than a path restated in two
# places that can drift apart.
CANVAS_TOOLCHAIN_DIR="${CANVAS_TOOLCHAIN_DIR:-$HOME/.unity/canvas-toolchain}"
CANVAS_HOST_DIR="${CANVAS_HOST_DIR:-$HOME/.unity/canvas-host}"

# Which branding checkout to build from. Console vendors it as a submodule, so a
# stack with console already has one; a sibling checkout wins for anyone working
# on the kit itself.
_canvas_branding_src() {
    local candidate
    for candidate in "${BRANDING_REPO:-}" "$UNITY_HOME/branding" "$CONSOLE_REPO/branding"; do
        if [[ -n "$candidate" && -f "$candidate/packages/canvas-kit/package.json" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

ensure_canvas_toolchain() {
    local installer="$UNITY_REPO/scripts/install-canvas-toolchain.sh"
    if [[ ! -f "$installer" ]]; then
        log_warn "Canvas installer not found at $installer — skipping canvas setup."
        return 0
    fi

    local branding
    if ! branding="$(_canvas_branding_src)"; then
        log_warn "No branding checkout found — canvas authoring will be unavailable."
        log_info "Initialise it with: git -C \"$CONSOLE_REPO\" submodule update --init branding"
        return 0
    fi

    if [[ -d "$CANVAS_TOOLCHAIN_DIR/node_modules" && -f "$CANVAS_HOST_DIR/host/v1/index.html" ]]; then
        log_success "Canvas toolchain already installed"
    else
        log_info "Building the canvas toolchain and runtime host (first run takes a few minutes)..."
        if ! bash "$installer" \
            --branding "$branding" \
            --toolchain "$CANVAS_TOOLCHAIN_DIR" \
            --host "$CANVAS_HOST_DIR"; then
            # Not fatal. The rest of the stack works without canvas, and failing
            # the whole setup over one optional surface is the wrong trade.
            log_warn "Canvas toolchain build failed — canvas authoring will be unavailable."
            return 0
        fi
        log_success "Canvas toolchain installed"
    fi

    # The render gate drives chromium through unify's own playwright. When it is
    # missing the gate skips rather than fails, so this is attempted and never
    # enforced.
    local py="$UNITY_REPO/.venv/bin/python"
    if [[ -x "$py" ]] && ! "$py" - <<'PY' >/dev/null 2>&1
import pathlib
import sys

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    sys.exit(0 if pathlib.Path(playwright.chromium.executable_path).exists() else 1)
PY
    then
        log_info "Installing Chromium for the canvas render gate..."
        "$py" -m playwright install chromium >/dev/null 2>&1 \
            || log_warn "Chromium install failed — canvases will publish without a visual check."
    fi
}

# --- Main -----------------------------------------------------------------
main() {
    local boot_runtime="false"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --boot-runtime) boot_runtime="true"; shift ;;
            -h|--help)
                echo "Usage: unity setup [--boot-runtime]"
                echo ""
                echo "  Bootstraps local Orchestra, wires unity/.env, and enables background scheduling."
                echo "  --boot-runtime  Also install a login hook so the runtime survives reboot."
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Run: unity setup --help"
                exit 1
                ;;
        esac
    done

    echo ""
    echo -e "${BOLD}Unity setup${NC} — bootstrapping local orchestra + Cloud voice"
    echo ""

    if [ ! -d "$UNITY_REPO" ]; then
        log_error "Unity is not installed at $UNITY_REPO. Run scripts/install.sh first."
        exit 1
    fi

    _load_install_progress

    if [ "${UNITY_SKIP_ORCHESTRA:-0}" = "1" ]; then
        log_warn "UNITY_SKIP_ORCHESTRA=1 — skipping orchestra spin-up."
        log_info "Set ORCHESTRA_URL + UNIFY_KEY manually in $UNITY_REPO/.env to point at a remote backend."
        exit 0
    fi

    ensure_docker || exit 1
    ensure_uv || exit 1
    if [[ -f "$SCRIPT_DIR/ensure_prereqs.sh" ]]; then
        # shellcheck disable=SC1090
        source "$SCRIPT_DIR/ensure_prereqs.sh"
        ensure_self_host_stack_prereqs || exit 1
    fi

    progress_step_begin 3 "Syncing orchestra repo (branch $UNITY_BRANCH)"
    progress_step_update 40
    if ! ensure_orchestra_repo; then
        progress_step_end_fail
        exit 1
    fi
    progress_step_end_success
    progress_repo_line "orchestra" "$(git -C "$ORCHESTRA_REPO" rev-parse --short HEAD)"

    if ! progress_step_run 4 "Installing orchestra Python dependencies (uv)" \
        install_orchestra_deps; then
        exit 1
    fi

    progress_step_begin 5 "Starting local orchestra (Docker + migrations)"
    if ! start_local_orchestra; then
        progress_step_end_fail
        exit 1
    fi
    progress_step_end_success

    wire_unity_env
    bootstrap_console_env

    if ! progress_step_run 6 "Installing Console npm dependencies" \
        ensure_console_npm_deps; then
        exit 1
    fi

    # Never gates setup: ensure_canvas_toolchain warns and returns 0 on every
    # failure path, because a stack without canvas is still a working stack.
    progress_step_run 7 "Installing the canvas authoring toolchain" \
        ensure_canvas_toolchain || true

    if ! progress_step_run 8 "Checking LiveKit Cloud voice configuration" \
        setup_voice_defaults; then
        exit 1
    fi

    if [[ -x "$SCRIPT_DIR/prompt_byok_keys.sh" ]]; then
        if has_env_value UNITY_BYOK_CONFIGURED; then
            log_success "BYOK already configured — skipping wizard"
        else
            echo ""
            log_info "BYOK wizard (LLM, voice, optional workspace OAuth)..."
            UNITY_REPO="$UNITY_REPO" bash "$SCRIPT_DIR/prompt_byok_keys.sh" || true
        fi
    fi

    if [[ -f "$SCRIPT_DIR/self_host_env.sh" ]]; then
        # shellcheck disable=SC1090
        source "$SCRIPT_DIR/self_host_env.sh"
        self_host_enable_runtime
        if [[ "$boot_runtime" == "true" && -x "$SCRIPT_DIR/service.sh" ]]; then
            log_info "Installing login boot hook for background runtime..."
            bash "$SCRIPT_DIR/service.sh" install-boot --boot || \
                log_warn "Boot hook install failed — stack up still enables background scheduling"
        fi
    fi

    echo ""
    echo -e "${GREEN}${BOLD}Setup complete.${NC}"
    echo ""
    echo "  orchestra is running at $UNIFY_BASE_URL"
    echo "  Stop it any time with:  unity stop"
    echo ""
    echo "  Next:  ${CYAN}unity stack doctor${NC}  Check self-host prerequisites (optional)"
    echo "         ${CYAN}unity stack up${NC}     Console + Coordinator (scheduled tasks enabled)"
    echo "         ${CYAN}unity stack smoke${NC}  Verify the running local product"
    echo "         ${CYAN}unity stack down${NC}   UI off; tasks keep running"
    echo "  Dev REPL:  ${CYAN}unity sandbox${NC}"
    if [[ "$boot_runtime" != "true" ]]; then
        echo ""
        echo "  Optional: ${CYAN}unity setup --boot-runtime${NC}  Keep scheduled tasks across reboot"
    fi
    echo ""
}

main "$@"
