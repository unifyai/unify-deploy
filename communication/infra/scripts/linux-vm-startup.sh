#!/bin/bash
# =============================================================================
# Linux VM Startup Script
# Equivalent to: windows-vm-startup.ps1
#
# This script runs on VM boot via GCP startup-script metadata and:
# 1. Reads configuration from GCP metadata
# 2. Configures VNC password
# 3. Updates/installs Magnitude and Agent Service
# 4. Configures Caddy reverse proxy
# 5. Starts all services via supervisord
#
# GCP Metadata Keys:
#   vnc-password      - VNC password (default: unify123)
#   hostname          - DNS hostname for Caddy HTTPS (e.g., vm.example.com)
#   github-token      - GitHub PAT for cloning private repos
#   anthropic-api-key - Anthropic API key for agent service
#   unify-key         - Unify API key for agent service
#   unify-base-url    - Unify API base URL for agent service
#   staging           - Use staging branch (any value = true)
#
# Usage (GCP):
#   gcloud compute instances create VM_NAME \
#     --image-family=unity-linux-vm \
#     --image-project=YOUR_PROJECT \
#     --metadata-from-file=startup-script=linux-vm-startup.sh \
#     --metadata=vnc-password=xxx,hostname=vm.example.com,...
# =============================================================================

set -e

# Ensure running as root (GCP startup scripts should run as root, but just in case)
if [[ $EUID -ne 0 ]]; then
    echo "Re-executing as root..."
    exec sudo bash "$0" "$@"
fi

METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
METADATA_HEADER="Metadata-Flavor: Google"

START_TIME=$(date +%s)

# Source environment
source /etc/profile.d/unity-vm.sh 2>/dev/null || true
source /etc/profile.d/bun.sh 2>/dev/null || true
export HOME=/root
export PATH="/root/.bun/bin:$PATH"

echo "=========================================="
echo "  Linux VM Startup Script"
echo "=========================================="
echo ""

# =============================================================================
# GCP Metadata Helper
# =============================================================================
get_metadata() {
    local key=$1
    curl -sf -H "$METADATA_HEADER" "$METADATA_URL/$key" 2>/dev/null || echo ""
}

# =============================================================================
# Read Configuration from GCP Metadata
# =============================================================================
echo "Reading GCP metadata..."

VNC_PASSWORD=$(get_metadata "vnc-password")
CONFIG_HOSTNAME=$(get_metadata "hostname")
GITHUB_TOKEN=$(get_metadata "github-token")
ANTHROPIC_API_KEY=$(get_metadata "anthropic-api-key")
UNIFY_KEY=$(get_metadata "unify-key")
UNIFY_BASE_URL=$(get_metadata "unify-base-url")
STAGING=$(get_metadata "staging")

# Defaults
VNC_PASSWORD=${VNC_PASSWORD:-"unify123"}

echo ""
echo "Configuration:"
echo "  VNC Password:   ******"
echo "  Hostname:       ${CONFIG_HOSTNAME:-'(not configured - no HTTPS)'}"
echo "  GitHub Token:   ${GITHUB_TOKEN:+(set)}"
echo "  Anthropic Key:  ${ANTHROPIC_API_KEY:+(set)}"
echo "  Unify Key:      ${UNIFY_KEY:+(set)}"
echo "  Staging Branch: ${STAGING:-no}"
echo ""

# =============================================================================
# Fast Mode Detection
# =============================================================================
fast_mode_check() {
    local checks=(
        "/magnitude/node_modules"
        "/agent-service/node_modules"
        "/novnc/vnc.html"
    )
    
    for path in "${checks[@]}"; do
        if [[ ! -e "$path" ]]; then
            echo "  Missing: $path"
            return 1
        fi
    done
    return 0
}

echo "Checking installation status..."
if fast_mode_check; then
    echo ""
    echo ">>> FAST MODE: All software pre-installed <<<"
    echo "    Updating repos and starting services only"
    FAST_MODE=true
else
    echo ""
    echo ">>> NORMAL MODE: Running full setup <<<"
    FAST_MODE=false
fi

# =============================================================================
# Configure VNC Password
# =============================================================================
echo ""
echo "=== Configuring VNC ==="
mkdir -p /root/.vnc

# Generate VNC password file using Python
VNC_PASSWORD="$VNC_PASSWORD" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES

def vnc_encrypt(password):
    # Bit-reversed VNC key for use with standard DES libraries
    key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
    # Password padded/truncated to 8 bytes
    pw = (password + '\x00' * 8)[:8].encode('latin-1')
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(pw)

password = os.environ.get('VNC_PASSWORD', 'unify123')
with open('/root/.vnc/passwd', 'wb') as f:
    f.write(vnc_encrypt(password))
os.chmod('/root/.vnc/passwd', 0o600)
PYSCRIPT

echo "VNC password configured"

# =============================================================================
# Repository Update Functions
# =============================================================================
get_pkg_hash() {
    local dir=$1
    if [[ -f "$dir/package.json" ]]; then
        md5sum "$dir/package.json" 2>/dev/null | cut -c1-8
    fi
}

save_pkg_hash() {
    local dir=$1
    local hash=$(get_pkg_hash "$dir")
    if [[ -n "$hash" ]]; then
        echo "$hash" > "$dir/.pkg-hash"
    fi
}

check_deps_installed() {
    local dir=$1
    
    # Check if node_modules exists
    if [[ ! -d "$dir/node_modules" ]]; then
        return 1
    fi
    
    # Check if hash matches
    local saved_hash=$(cat "$dir/.pkg-hash" 2>/dev/null || echo "")
    local current_hash=$(get_pkg_hash "$dir")
    
    if [[ "$saved_hash" == "$current_hash" && -n "$saved_hash" ]]; then
        return 0
    fi
    
    return 1
}

update_repo() {
    local dir=$1
    local branch=$2
    local repo_url=$3
    local name=$4
    
    if [[ -d "$dir/.git" ]]; then
        echo "Updating $name (git fetch)..."
        cd "$dir"
        
        # Update remote URL if token provided
        if [[ -n "$GITHUB_TOKEN" ]]; then
            git remote set-url origin "$repo_url" 2>/dev/null || true
        fi
        
        git fetch --depth 1 origin "$branch" 2>&1 || true
        git reset --hard "origin/$branch" 2>&1 || true
        
        local commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
        echo "  Updated to commit: $commit"
        
        # Check if dependencies need reinstall
        if ! check_deps_installed "$dir"; then
            echo "  Dependencies changed, reinstalling..."
            if command -v bun &>/dev/null; then
                bun install 2>&1 || npm install 2>&1
            else
                npm install 2>&1
            fi
            save_pkg_hash "$dir"
            echo "  Dependencies updated"
        else
            echo "  Dependencies unchanged, skipping install"
        fi
    else
        echo "$name exists but no .git directory"
    fi
}

# =============================================================================
# Update/Install Magnitude
# =============================================================================
echo ""
echo "=== Updating Magnitude ==="

if [[ -n "$GITHUB_TOKEN" ]]; then
    MAGNITUDE_URL="https://${GITHUB_TOKEN}@github.com/unifyai/magnitude.git"
else
    MAGNITUDE_URL="https://github.com/unifyai/magnitude.git"
fi

if [[ -d "/magnitude/.git" ]]; then
    update_repo /magnitude unity-modifications "$MAGNITUDE_URL" "Magnitude"
elif [[ -f "/magnitude/package.json" ]]; then
    echo "Magnitude exists (no .git), checking dependencies..."
    if ! check_deps_installed /magnitude; then
        echo "  Installing dependencies..."
        cd /magnitude
        if command -v bun &>/dev/null; then
            bun install 2>&1 || npm install 2>&1
        else
            npm install 2>&1
        fi
        save_pkg_hash /magnitude
    else
        echo "  Dependencies up to date"
    fi
else
    echo "Magnitude not found, cloning..."
    git clone --depth 1 --branch unity-modifications "$MAGNITUDE_URL" /magnitude 2>&1
    cd /magnitude
    if command -v bun &>/dev/null; then
        bun install 2>&1 || npm install 2>&1
    else
        npm install 2>&1
    fi
    save_pkg_hash /magnitude
    echo "Magnitude installed"
fi

# =============================================================================
# Update/Install Agent Service
# =============================================================================
echo ""
echo "=== Updating Agent Service ==="

UNITY_BRANCH="main"
if [[ -n "$STAGING" ]]; then
    UNITY_BRANCH="staging"
    echo "Using staging branch"
fi

if [[ -n "$GITHUB_TOKEN" ]]; then
    UNITY_URL="https://${GITHUB_TOKEN}@github.com/unifyai/unity.git"
else
    UNITY_URL="https://github.com/unifyai/unity.git"
fi

if [[ ! -f "/agent-service/package.json" ]]; then
    echo "Installing Agent Service from Unity repo..."
    
    # Sparse checkout agent-service from unity repo
    tmp_dir=$(mktemp -d)
    echo "  Cloning unity repo (sparse)..."
    git clone --depth 1 --branch "$UNITY_BRANCH" --filter=blob:none --sparse "$UNITY_URL" "$tmp_dir" 2>&1
    
    cd "$tmp_dir"
    git sparse-checkout set agent-service 2>&1
    
    # Move to final location
    rm -rf /agent-service
    mv agent-service /agent-service
    
    # Save commit hash for tracking
    commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
    echo "$commit" > /agent-service/.commit-hash
    
    rm -rf "$tmp_dir"
    
    echo "  Installing dependencies..."
    cd /agent-service
    if command -v bun &>/dev/null; then
        bun install 2>&1 || npm install 2>&1
    else
        npm install 2>&1
    fi
    
    # Install Playwright browsers
    echo "  Installing Playwright Chromium..."
    npx playwright@1.52.0 install --with-deps chromium 2>&1 || true
    
    save_pkg_hash /agent-service
    echo "Agent Service installed (commit: $commit)"
else
    echo "Agent Service exists, checking dependencies..."
    if ! check_deps_installed /agent-service; then
        echo "  Installing dependencies..."
        cd /agent-service
        if command -v bun &>/dev/null; then
            bun install 2>&1 || npm install 2>&1
        else
            npm install 2>&1
        fi
        save_pkg_hash /agent-service
    else
        echo "  Dependencies up to date"
    fi
fi

# =============================================================================
# Configure Agent Service Environment
# =============================================================================
echo ""
echo "=== Configuring Agent Service Environment ==="

cat > /agent-service/.env << EOF
# Agent Service Environment Configuration
# Generated: $(date)

PORT=3000
NODE_ENV=production
EOF

if [[ -n "$ANTHROPIC_API_KEY" ]]; then
    echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" >> /agent-service/.env
    echo "  ANTHROPIC_API_KEY: (set)"
else
    echo "  ANTHROPIC_API_KEY: (not provided)"
fi

if [[ -n "$UNIFY_KEY" ]]; then
    echo "UNIFY_KEY=$UNIFY_KEY" >> /agent-service/.env
    echo "  UNIFY_KEY: (set)"
else
    echo "  UNIFY_KEY: (not provided)"
fi

if [[ -n "$UNIFY_BASE_URL" ]]; then
    echo "UNIFY_BASE_URL=$UNIFY_BASE_URL" >> /agent-service/.env
    echo "  UNIFY_BASE_URL: $UNIFY_BASE_URL"
fi

echo ".env file configured"

# =============================================================================
# Configure Caddy
# =============================================================================
echo ""
echo "=== Configuring Caddy ==="

mkdir -p /etc/caddy
mkdir -p /var/log/caddy

if [[ -n "$CONFIG_HOSTNAME" ]]; then
    cat > /etc/caddy/Caddyfile << EOF
# Linux VM - Caddy Configuration
# Hostname: $CONFIG_HOSTNAME
# Generated: $(date)

$CONFIG_HOSTNAME {
    # Handle WebSocket upgrade for noVNC
    @websocket {
        path /desktop/*
        header Connection *Upgrade*
        header Upgrade websocket
    }
    reverse_proxy @websocket localhost:6080

    # Exact match for /desktop (redirect to /desktop/)
    @desktop_exact path /desktop
    handle @desktop_exact {
        redir /desktop/ permanent
    }

    # noVNC desktop at /desktop/*
    handle_path /desktop/* {
        reverse_proxy localhost:6080
    }

    # Exact match for /api (redirect to /api/)
    @api_exact path /api
    handle @api_exact {
        redir /api/ permanent
    }

    # Agent Service API at /api/*
    handle_path /api/* {
        reverse_proxy localhost:3000
    }

    # Block all other paths
    handle {
        respond "Not Found" 404
    }

    # Logging
    log {
        output file /var/log/caddy/access.log {
            roll_size 10mb
            roll_keep 3
        }
        format console
    }
}
EOF
    echo "Caddy configured for https://$CONFIG_HOSTNAME"
    echo "  /desktop/* -> noVNC (localhost:6080)"
    echo "  /api/*     -> Agent Service (localhost:3000)"
    CADDY_ENABLED=true
else
    echo "No hostname provided, Caddy will not start"
    echo "Access services directly:"
    echo "  noVNC:         http://<ip>:6080"
    echo "  Agent Service: http://<ip>:3000"
    CADDY_ENABLED=false
    
    # Create empty Caddyfile to prevent errors
    echo "# No hostname configured" > /etc/caddy/Caddyfile
fi

# =============================================================================
# Summary
# =============================================================================
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo ""
echo "=========================================="
echo "  Startup Complete!"
echo "=========================================="
echo ""
echo "Installed components:"
echo "  - XFCE4 Desktop"
echo "  - TigerVNC Server (port 5901)"
echo "  - noVNC + websockify (port 6080)"
if [[ -f "/magnitude/package.json" ]]; then
    echo "  - Magnitude (unity-modifications)"
fi
if [[ -f "/agent-service/package.json" ]]; then
    echo "  - Agent Service (port 3000)"
fi
if [[ "$CADDY_ENABLED" == "true" ]]; then
    echo "  - Caddy HTTPS reverse proxy (ports 80, 443)"
fi
echo ""
echo "Paths:"
echo "  noVNC:         /novnc"
echo "  Magnitude:     /magnitude"
echo "  Agent Service: /agent-service"
echo ""

# Get IP address for display
IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

if [[ "$CADDY_ENABLED" == "true" ]]; then
    echo "HTTPS Access (via Caddy):"
    echo "  Desktop: https://$CONFIG_HOSTNAME/desktop/"
    echo "  API:     https://$CONFIG_HOSTNAME/api/"
    echo ""
    echo "Direct HTTP Access (fallback):"
    echo "  Desktop: http://$IP_ADDR:6080"
    echo "  API:     http://$IP_ADDR:3000/"
else
    echo "HTTP Access:"
    echo "  Desktop: http://$IP_ADDR:6080"
    echo "  API:     http://$IP_ADDR:3000/"
fi
echo ""
echo "VNC Password: $VNC_PASSWORD"
echo ""
echo "Total startup time: ${ELAPSED} seconds"
if [[ "$FAST_MODE" == "true" ]]; then
    echo "Boot mode: FAST"
else
    echo "Boot mode: NORMAL (full setup)"
fi
echo ""

# =============================================================================
# Start Services via supervisord
# =============================================================================
echo "Starting services via supervisord..."

# Export environment variables for supervisord
export VNC_GEOMETRY=${VNC_GEOMETRY:-1920x1080}
export VNC_DEPTH=${VNC_DEPTH:-24}

# Start supervisord in foreground (keeps the script running)
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/unity-vm.conf

