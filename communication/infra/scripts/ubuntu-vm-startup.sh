#!/bin/bash
# =============================================================================
# Ubuntu VM Startup Script
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
#   unify-key         - Unify API key for agent service
#   orchestra-url     - Orchestra API base URL for agent service
#   comms-url         - Communication service base URL for agent service
#   staging           - Use staging branch (any value = true)
#   ssh-username      - SSH username for file sync (e.g., "42" — the assistant's agent_id)
#   ssh-public-key    - SSH public key for file sync (Ed25519)
#
# Usage (GCP):
#   gcloud compute instances create VM_NAME \
#     --image-family=unity-ubuntu-vm \
#     --image-project=YOUR_PROJECT \
#     --metadata-from-file=startup-script=ubuntu-vm-startup.sh \
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
echo "  Ubuntu VM Startup Script"
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
UNIFY_KEY=$(get_metadata "unify-key")
ORCHESTRA_URL=$(get_metadata "orchestra-url")
COMMS_URL=$(get_metadata "comms-url")
STAGING=$(get_metadata "staging")
SSH_USERNAME=$(get_metadata "ssh-username")
SSH_PUBLIC_KEY=$(get_metadata "ssh-public-key")

# Defaults
VNC_PASSWORD=${VNC_PASSWORD:-"unify123"}

echo ""
echo "Configuration:"
echo "  VNC Password:   ******"
echo "  Hostname:       ${CONFIG_HOSTNAME:-'(not configured - no HTTPS)'}"
echo "  GitHub Token:   ${GITHUB_TOKEN:+(set)}"
echo "  Unify Key:      ${UNIFY_KEY:+(set)}"
echo "  Orchestra URL:  ${ORCHESTRA_URL:-(not configured)}"
echo "  Comms URL:      ${COMMS_URL:-(not configured)}"
echo "  Staging Branch: ${STAGING:-no}"
echo "  SSH Username:   ${SSH_USERNAME:-(not configured)}"
echo "  SSH Public Key: ${SSH_PUBLIC_KEY:+(set)}"
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
# SSH File Sync User Setup
# =============================================================================
setup_ssh_file_sync() {
    echo ""
    echo "=== Configuring SSH File Sync ==="

    if [[ -z "$SSH_USERNAME" || -z "$SSH_PUBLIC_KEY" ]]; then
        echo "SSH file sync not configured (missing username or public key)"
        return
    fi

    echo "Setting up SSH file sync for user: $SSH_USERNAME"

    # 1. Create user with /Unity as home directory (if doesn't exist)
    if ! id "$SSH_USERNAME" &>/dev/null; then
        # Create the user with /Unity as home, no password (SSH key only)
        useradd -m -d /Unity -s /bin/bash "$SSH_USERNAME" 2>/dev/null || true
        echo "  Created user: $SSH_USERNAME"
    else
        echo "  User $SSH_USERNAME already exists"
        # Ensure home directory is /Unity
        usermod -d /Unity "$SSH_USERNAME" 2>/dev/null || true
    fi

    # 2. Create /Unity/Local directory for file sync
    # Note: Subdirectories (Downloads, user_files, etc.) are created by sync process
    mkdir -p /Unity/Local
    chown -R "$SSH_USERNAME:$SSH_USERNAME" /Unity
    chmod 755 /Unity
    chmod 755 /Unity/Local
    echo "  Created /Unity/Local sync directory"

    # 3. Setup SSH authorized_keys for the user
    mkdir -p /Unity/.ssh
    echo "$SSH_PUBLIC_KEY" > /Unity/.ssh/authorized_keys
    chown -R "$SSH_USERNAME:$SSH_USERNAME" /Unity/.ssh
    chmod 700 /Unity/.ssh
    chmod 600 /Unity/.ssh/authorized_keys
    echo "  Configured SSH authorized_keys"

    # 4. Configure SSHD for file sync on port 2222
    # Check if we already configured port 2222
    if ! grep -q "^Port 2222" /etc/ssh/sshd_config; then
        # Backup original config
        cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak

        # Add port 2222 and SFTP configuration for file sync user
        # Keep port 22 for normal SSH access, add 2222 for file sync
        cat >> /etc/ssh/sshd_config << SSHEOF

# =============================================================================
# Unity File Sync - SSH on port 2222
# Generated by ubuntu-vm-startup.sh
# =============================================================================
Port 22
Port 2222

# SFTP-only access for file sync user
Match User $SSH_USERNAME
    ForceCommand internal-sftp
    ChrootDirectory /
    AllowTcpForwarding no
    X11Forwarding no
    PasswordAuthentication no
SSHEOF

        echo "  Added SSHD configuration for port 2222"
    else
        echo "  SSHD port 2222 already configured"
    fi

    # 5. Restart SSHD to apply changes
    systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
    echo "  Restarted SSHD"

    echo "SSH file sync configured:"
    echo "  User: $SSH_USERNAME"
    echo "  Port: 2222"
    echo "  Sync Path: /Unity/Local"
    echo "  Mode: SFTP-only"
}

# Run SSH setup
setup_ssh_file_sync

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

get_remote_commit_hash() {
    local repo_url=$1
    local branch=$2
    git ls-remote "$repo_url" "refs/heads/$branch" 2>/dev/null | cut -c1-12
}

get_saved_commit_hash() {
    local dir=$1
    cat "$dir/.commit-hash" 2>/dev/null || echo ""
}

save_commit_hash() {
    local dir=$1
    local hash=$2
    if [[ -n "$hash" ]]; then
        echo -n "$hash" > "$dir/.commit-hash"
    fi
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
elif [[ ! -f "/magnitude/package.json" ]]; then
    echo "Magnitude not found, cloning..."
    git clone --depth 1 --branch unity-modifications "$MAGNITUDE_URL" /magnitude 2>&1
    echo "Magnitude cloned"
fi

# Always run bun install to ensure node_modules matches the current code
echo "  Installing Magnitude dependencies..."
cd /magnitude
if command -v bun &>/dev/null; then
    bun install 2>&1
else
    npm install 2>&1
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
    save_commit_hash /agent-service "$commit"

    rm -rf "$tmp_dir"

    echo "  Installing dependencies..."
    cd /agent-service
    npm install 2>&1

    # Install Patchright browsers (from magnitude-core's node_modules)
    echo "  Installing Patchright Chromium..."
    cd /magnitude/packages/magnitude-core && npx patchright install --with-deps chromium 2>&1 || true

    echo "Agent Service installed (commit: $commit)"
else
    # Agent Service exists - check for code updates via remote commit hash
    saved_hash=$(get_saved_commit_hash /agent-service)
    remote_hash=$(get_remote_commit_hash "$UNITY_URL" "$UNITY_BRANCH")

    needs_update=true
    if [[ -n "$saved_hash" && -n "$remote_hash" && "$saved_hash" == "$remote_hash" ]]; then
        echo "Agent Service up-to-date (commit: $saved_hash)"
        needs_update=false
    elif [[ -n "$saved_hash" && -n "$remote_hash" ]]; then
        echo "Agent Service update available ($saved_hash -> $remote_hash)"
    fi

    if [[ "$needs_update" == "true" ]]; then
        echo "Updating Agent Service from Unity repo..."

        # Re-clone via sparse checkout
        tmp_dir=$(mktemp -d)
        echo "  Cloning unity repo (sparse)..."
        git clone --depth 1 --branch "$UNITY_BRANCH" --filter=blob:none --sparse "$UNITY_URL" "$tmp_dir" 2>&1

        cd "$tmp_dir"
        git sparse-checkout set agent-service 2>&1

        # Get new commit hash
        commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")

        # Replace agent-service directory
        rm -rf /agent-service
        mv agent-service /agent-service

        rm -rf "$tmp_dir"

        save_commit_hash /agent-service "$commit"
    fi

    # Always run npm install to ensure node_modules matches the current code
    echo "  Installing Agent Service dependencies..."
    cd /agent-service
    npm install 2>&1
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

if [[ -n "$UNIFY_KEY" ]]; then
    echo "UNIFY_KEY=$UNIFY_KEY" >> /agent-service/.env
    echo "  UNIFY_KEY: (set)"
else
    echo "  UNIFY_KEY: (not provided)"
fi

if [[ -n "$ORCHESTRA_URL" ]]; then
    echo "ORCHESTRA_URL=$ORCHESTRA_URL" >> /agent-service/.env
    echo "  ORCHESTRA_URL: $ORCHESTRA_URL"
fi

if [[ -n "$COMMS_URL" ]]; then
    echo "UNITY_COMMS_URL=$COMMS_URL" >> /agent-service/.env
    echo "  UNITY_COMMS_URL: $COMMS_URL"
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
# Ubuntu VM - Caddy Configuration
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
if [[ -n "$SSH_USERNAME" && -n "$SSH_PUBLIC_KEY" ]]; then
    echo "  - SSH File Sync (port 2222, user: $SSH_USERNAME)"
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
