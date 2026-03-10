#!/bin/bash
# =============================================================================
# Ubuntu VM Startup Script (Pool VMs)
#
# Runs on every boot via GCP startup-script metadata. Handles pool-level
# infrastructure only - per-assistant configuration (SSH keys, VNC password,
# API keys, disk mount, Agent Service start) is handled by the pool watcher.
#
# Responsibilities:
#   1. Install runtime deps not baked into image
#   2. Update pool watcher script from metadata
#   3. Configure VNC default password (for supervisord to start TigerVNC)
#   4. Update Magnitude and Agent Service code
#   5. Configure Caddy reverse proxy (hostname + TLS)
#   6. Mark pool VM as idle
#   7. Start supervisord
#
# GCP Metadata Keys (set at pool creation):
#   hostname, github-token, orchestra-url, comms-url, staging,
#   tls-fullchain, tls-privkey, pool-watcher-script
#
# GCP Metadata Keys (set at assignment, handled by pool watcher):
#   unify-key, vnc-password, ssh-public-key, disk-device, assistant-id
# =============================================================================

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
METADATA_HEADER="Metadata-Flavor: Google"
START_TIME=$(date +%s)

source /etc/profile.d/unity-vm.sh 2>/dev/null || true
source /etc/profile.d/bun.sh 2>/dev/null || true
export HOME=/root
export PATH="/root/.bun/bin:$PATH"

echo "=========================================="
echo "  Ubuntu VM Startup Script"
echo "=========================================="

# =============================================================================
# Helpers
# =============================================================================
get_metadata() {
    curl -sf -H "$METADATA_HEADER" "$METADATA_URL/$1" 2>/dev/null || echo ""
}

get_remote_commit_hash() {
    git ls-remote "$1" "refs/heads/$2" 2>/dev/null | cut -c1-12
}

save_commit_hash() {
    [[ -n "$2" ]] && echo -n "$2" > "$1/.commit-hash"
}

get_saved_commit_hash() {
    cat "$1/.commit-hash" 2>/dev/null || echo ""
}

# =============================================================================
# Runtime dependencies not baked into image
# =============================================================================
apt-get update -qq
apt-get install -y --no-install-recommends xdotool

# =============================================================================
# Read Configuration
# =============================================================================
echo ""
echo "Reading GCP metadata..."
CONFIG_HOSTNAME=$(get_metadata "hostname")
GITHUB_TOKEN=$(get_metadata "github-token")
STAGING=$(get_metadata "staging")
TLS_FULLCHAIN=$(get_metadata "tls-fullchain")
TLS_PRIVKEY=$(get_metadata "tls-privkey")

echo "  Hostname:       ${CONFIG_HOSTNAME:-(not configured)}"
echo "  GitHub Token:   ${GITHUB_TOKEN:+(set)}"
echo "  Staging Branch: ${STAGING:-no}"
echo "  TLS Wildcard:   ${TLS_FULLCHAIN:+(set)}"

# =============================================================================
# Update Pool Watcher from Metadata
# =============================================================================
WATCHER_PATH="/usr/local/bin/unity-pool-watcher.sh"
if curl -sf -H "$METADATA_HEADER" "$METADATA_URL/pool-watcher-script" \
    -o "$WATCHER_PATH" 2>/dev/null && [[ -s "$WATCHER_PATH" ]]; then
    chmod +x "$WATCHER_PATH"
    systemctl restart unity-pool-watcher.service 2>/dev/null || true
    echo "Pool watcher updated from metadata"
else
    echo "No pool-watcher-script metadata, using baked-in version"
fi

# =============================================================================
# VNC Default Password (so supervisord can start TigerVNC)
# =============================================================================
mkdir -p /root/.vnc
VNC_PASSWORD="unify123" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
pw = (os.environ.get('VNC_PASSWORD', 'unify123') + '\x00' * 8)[:8].encode('latin-1')
with open('/root/.vnc/passwd', 'wb') as f:
    f.write(DES.new(key, DES.MODE_ECB).encrypt(pw))
os.chmod('/root/.vnc/passwd', 0o600)
PYSCRIPT
echo "VNC default password configured"

# =============================================================================
# Build repo URLs
# =============================================================================
if [[ -n "$GITHUB_TOKEN" ]]; then
    MAGNITUDE_URL="https://${GITHUB_TOKEN}@github.com/unifyai/magnitude.git"
    UNITY_URL="https://${GITHUB_TOKEN}@github.com/unifyai/unity.git"
else
    MAGNITUDE_URL="https://github.com/unifyai/magnitude.git"
    UNITY_URL="https://github.com/unifyai/unity.git"
fi
UNITY_BRANCH="${STAGING:+staging}"
UNITY_BRANCH="${UNITY_BRANCH:-main}"

# =============================================================================
# Update Magnitude
# =============================================================================
echo ""
echo "=== Updating Magnitude ==="

if [[ -d "/magnitude/.git" ]]; then
    cd /magnitude
    [[ -n "$GITHUB_TOKEN" ]] && git remote set-url origin "$MAGNITUDE_URL" 2>/dev/null || true
    git fetch --depth 1 origin unity-modifications 2>&1 || true
    git reset --hard origin/unity-modifications 2>&1 || true
    commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
    save_commit_hash /magnitude "$commit"
    echo "  Magnitude updated (commit: $commit)"
elif [[ ! -f "/magnitude/package.json" ]]; then
    echo "  Cloning Magnitude..."
    git clone --depth 1 --branch unity-modifications "$MAGNITUDE_URL" /magnitude 2>&1
fi

echo "  Installing dependencies..."
cd /magnitude
if command -v bun &>/dev/null; then
    bun install 2>&1
else
    npm install 2>&1
fi

# Install Patchright Chromium from magnitude-core
if [[ -f /magnitude/packages/magnitude-core/package.json ]]; then
    echo "  Installing Patchright Chromium..."
    cd /magnitude/packages/magnitude-core && npx --yes patchright install --with-deps chromium 2>&1 || true
    cd /
    echo "  Patchright Chromium installed"
fi

# =============================================================================
# Update Agent Service
# =============================================================================
echo ""
echo "=== Updating Agent Service ==="

saved_hash=$(get_saved_commit_hash /agent-service)
remote_hash=$(get_remote_commit_hash "$UNITY_URL" "$UNITY_BRANCH")
needs_update=true

if [[ -f "/agent-service/package.json" && -n "$saved_hash" && -n "$remote_hash" && "$saved_hash" == "$remote_hash" ]]; then
    echo "  Agent Service up-to-date (commit: $saved_hash)"
    needs_update=false
elif [[ -n "$saved_hash" && -n "$remote_hash" ]]; then
    echo "  Agent Service update available ($saved_hash -> $remote_hash)"
fi

if [[ "$needs_update" == "true" ]]; then
    tmp_dir=$(mktemp -d)
    git clone --depth 1 --branch "$UNITY_BRANCH" --filter=blob:none --sparse "$UNITY_URL" "$tmp_dir" 2>&1
    cd "$tmp_dir"
    git sparse-checkout set agent-service 2>&1
    commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")

    # Preserve node_modules to speed up npm install
    if [[ -d /agent-service/node_modules ]]; then
        mv /agent-service/node_modules "$tmp_dir/agent-service/node_modules"
    fi
    rm -rf /agent-service
    mv agent-service /agent-service
    save_commit_hash /agent-service "$commit"
    rm -rf "$tmp_dir"
    echo "  Agent Service updated (commit: $commit)"
fi

echo "  Installing dependencies..."
cd /agent-service
npm install 2>&1

# =============================================================================
# Configure Caddy
# =============================================================================
echo ""
echo "=== Configuring Caddy ==="

mkdir -p /etc/caddy /var/log/caddy

TLS_DIRECTIVE=""
if [[ -n "$TLS_FULLCHAIN" && -n "$TLS_PRIVKEY" ]]; then
    mkdir -p /etc/caddy/certs
    echo "$TLS_FULLCHAIN" > /etc/caddy/certs/fullchain.pem
    echo "$TLS_PRIVKEY" > /etc/caddy/certs/privkey.pem
    chmod 600 /etc/caddy/certs/privkey.pem
    TLS_DIRECTIVE="    tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem"
    echo "  TLS cert written"
fi

if [[ -n "$CONFIG_HOSTNAME" ]]; then
    cat > /etc/caddy/Caddyfile << EOF
$CONFIG_HOSTNAME {
$TLS_DIRECTIVE
    @websocket {
        path /desktop/*
        header Connection *Upgrade*
        header Upgrade websocket
    }
    reverse_proxy @websocket localhost:6080

    @desktop_exact path /desktop
    handle @desktop_exact {
        redir /desktop/ permanent
    }

    handle_path /desktop/* {
        reverse_proxy localhost:6080
    }

    @api_exact path /api
    handle @api_exact {
        redir /api/ permanent
    }

    handle_path /api/* {
        reverse_proxy localhost:3000
    }

    handle {
        respond "Not Found" 404
    }

    log {
        output file /var/log/caddy/access.log {
            roll_size 10mb
            roll_keep 3
        }
        format console
    }
}
EOF
    echo "  Caddy configured for https://$CONFIG_HOSTNAME"
else
    echo "# No hostname configured" > /etc/caddy/Caddyfile
    echo "  No hostname provided, Caddy disabled"
fi

# =============================================================================
# Mark pool VM as idle
# =============================================================================
TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || true)

if [[ -n "$TOKEN" ]]; then
    GCP_PROJECT=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/project/project-id")
    GCP_ZONE=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/zone" | awk -F/ '{print $NF}')
    GCP_INSTANCE=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/name")

    INFO=$(curl -sf -H "Authorization: Bearer $TOKEN" \
        "https://compute.googleapis.com/compute/v1/projects/$GCP_PROJECT/zones/$GCP_ZONE/instances/$GCP_INSTANCE")
    POOL_ROLE=$(echo "$INFO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('labels',{}).get('pool-role',''))" 2>/dev/null || true)

    if [[ "$POOL_ROLE" == "provisioning" || "$POOL_ROLE" == "stopped" ]]; then
        FINGERPRINT=$(echo "$INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['labelFingerprint'])")
        NEW_LABELS=$(echo "$INFO" | python3 -c "
import sys, json
labels = json.load(sys.stdin).get('labels', {})
labels['pool-role'] = 'idle'
print(json.dumps(labels))
")
        curl -sf -X POST \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            "https://compute.googleapis.com/compute/v1/projects/$GCP_PROJECT/zones/$GCP_ZONE/instances/$GCP_INSTANCE/setLabels" \
            -d "{\"labels\": $NEW_LABELS, \"labelFingerprint\": \"$FINGERPRINT\"}"
        echo "Pool VM marked as idle"
    fi
fi

# =============================================================================
# Start Services
# =============================================================================
ELAPSED=$(( $(date +%s) - START_TIME ))
echo ""
echo "Startup complete in ${ELAPSED}s - launching supervisord"

export VNC_GEOMETRY=${VNC_GEOMETRY:-1920x1080}
export VNC_DEPTH=${VNC_DEPTH:-24}
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/unity-vm.conf
