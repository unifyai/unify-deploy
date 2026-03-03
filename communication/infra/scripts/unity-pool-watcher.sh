#!/bin/bash
# =============================================================================
# Unity Pool Watcher
#
# Systemd service that watches GCE instance metadata for assignment/release
# signals. When unify-key changes from empty to non-empty, the VM is being
# assigned to an assistant; when it changes back to empty, the VM is released.
#
# Install as: /usr/local/bin/unity-pool-watcher.sh
# Systemd unit: /etc/systemd/system/unity-pool-watcher.service
# =============================================================================

set -euo pipefail

METADATA_URL="http://metadata.google.internal/computeMetadata/v1"
METADATA_HEADER="Metadata-Flavor: Google"
ETAG=""
PREV_UNIFY_KEY=""

get_metadata() {
    local key=$1
    curl -sf -H "$METADATA_HEADER" "$METADATA_URL/instance/attributes/$key" 2>/dev/null || echo ""
}

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

# ─── Assignment: configure VM for an assistant ───────────────────────────

do_assign() {
    local unify_key=$1
    log "ASSIGN: configuring VM for assistant"

    local vnc_password
    local ssh_public_key
    local disk_device
    local assistant_id
    local hostname
    local orchestra_url
    local comms_url

    vnc_password=$(get_metadata "vnc-password")
    ssh_public_key=$(get_metadata "ssh-public-key")
    disk_device=$(get_metadata "disk-device")
    assistant_id=$(get_metadata "assistant-id")
    hostname=$(get_metadata "hostname")
    orchestra_url=$(get_metadata "orchestra-url")
    comms_url=$(get_metadata "comms-url")

    # Mount persistent disk
    if [[ -n "$disk_device" ]]; then
        local dev_path="/dev/disk/by-id/google-${disk_device}"
        # Wait for device to appear (GCE disk attach can take a moment)
        for i in $(seq 1 30); do
            [[ -e "$dev_path" ]] && break
            sleep 1
        done

        if [[ -e "$dev_path" ]]; then
            # Format if no filesystem
            if ! blkid "$dev_path" &>/dev/null; then
                log "Formatting new disk: $dev_path"
                mkfs.ext4 -q "$dev_path"
            fi
            mkdir -p /Unity/Local
            mount "$dev_path" /Unity/Local
            chown unityuser:unityuser /Unity/Local
            chmod 755 /Unity/Local
            log "Mounted $dev_path at /Unity/Local"
        else
            log "WARNING: disk device $dev_path not found after 30s"
        fi
    fi

    # SSH authorized_keys
    if [[ -n "$ssh_public_key" ]]; then
        mkdir -p /Unity/.ssh
        echo "$ssh_public_key" > /Unity/.ssh/authorized_keys
        chown -R unityuser:unityuser /Unity/.ssh
        chmod 700 /Unity/.ssh
        chmod 600 /Unity/.ssh/authorized_keys
        log "SSH authorized_keys configured"
    fi

    # VNC password
    if [[ -n "$vnc_password" ]]; then
        VNC_PASSWORD="$vnc_password" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
def vnc_encrypt(password):
    key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
    pw = (password + '\x00' * 8)[:8].encode('latin-1')
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(pw)
password = os.environ.get('VNC_PASSWORD', 'unify123')
with open('/root/.vnc/passwd', 'wb') as f:
    f.write(vnc_encrypt(password))
os.chmod('/root/.vnc/passwd', 0o600)
PYSCRIPT
        log "VNC password updated (takes effect on next client connection)"
    fi

    # Agent Service .env
    cat > /agent-service/.env << EOF
PORT=3000
NODE_ENV=production
UNIFY_KEY=$unify_key
ORCHESTRA_URL=$orchestra_url
UNITY_COMMS_URL=$comms_url
EOF
    log "Agent Service .env configured"

    # Start Agent Service directly (bypass supervisord which may not be running)
    pkill -f "ts-node src/index.ts" 2>/dev/null || true
    sleep 1
    cd /agent-service
    nohup npx ts-node src/index.ts > /var/log/agent-service.log 2>&1 &
    echo $! > /var/run/agent-service.pid
    cd /
    log "Agent Service started (PID $(cat /var/run/agent-service.pid))"

    # Send ready notification
    if [[ -n "$comms_url" && -n "$hostname" && -n "$unify_key" && -n "$assistant_id" ]]; then
        # Retry the ready notification (Caddy may need a moment)
        for attempt in $(seq 1 10); do
            local http_code
            http_code=$(curl -sf -o /dev/null -w "%{http_code}" \
                -X POST "$comms_url/infra/vm/ready" \
                -H "Content-Type: application/json" \
                -H "Authorization: Bearer $unify_key" \
                -d "{\"assistant_id\": \"$assistant_id\", \"vm_type\": \"ubuntu\", \"hostname\": \"$hostname\"}" \
                2>/dev/null || echo "000")

            if [[ "$http_code" == "200" ]]; then
                log "VM ready notification sent (attempt $attempt)"
                break
            fi
            log "VM ready notification attempt $attempt failed (HTTP $http_code), retrying in 5s..."
            sleep 5
        done
    fi

    log "ASSIGN complete"
}

# ─── Release: clean up VM for return to pool ─────────────────────────────

do_release() {
    log "RELEASE: cleaning up VM"

    # Stop Agent Service via PID file, then fall back to pkill
    if [[ -f /var/run/agent-service.pid ]]; then
        local agent_pid
        agent_pid=$(cat /var/run/agent-service.pid)
        if kill -0 "$agent_pid" 2>/dev/null; then
            kill "$agent_pid" 2>/dev/null
            for i in $(seq 1 10); do
                kill -0 "$agent_pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$agent_pid" 2>/dev/null || true
        fi
        rm -f /var/run/agent-service.pid
    fi
    pkill -f "ts-node src/index.ts" 2>/dev/null || true
    sleep 1
    log "Agent Service stopped"

    # Clear .env
    rm -f /agent-service/.env
    log "Agent Service .env cleared"

    # Clear SSH keys
    rm -f /Unity/.ssh/authorized_keys
    log "SSH authorized_keys cleared"

    # Reset VNC password to dead value
    VNC_PASSWORD="disabled" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
def vnc_encrypt(password):
    key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
    pw = (password + '\x00' * 8)[:8].encode('latin-1')
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(pw)
password = os.environ.get('VNC_PASSWORD', 'disabled')
with open('/root/.vnc/passwd', 'wb') as f:
    f.write(vnc_encrypt(password))
os.chmod('/root/.vnc/passwd', 0o600)
PYSCRIPT
    log "VNC password reset"

    # Unmount persistent disk (kill busy processes first, then lazy fallback)
    if mountpoint -q /Unity/Local 2>/dev/null; then
        fuser -km /Unity/Local 2>/dev/null || true
        sleep 1
        if ! umount /Unity/Local 2>/dev/null; then
            umount -l /Unity/Local 2>/dev/null || true
            log "Lazy-unmounted /Unity/Local (was busy)"
        else
            log "Unmounted /Unity/Local"
        fi
    fi

    log "RELEASE complete"
}

# ─── Main watcher loop ───────────────────────────────────────────────────

log "Unity Pool Watcher starting"

# Read initial state
PREV_UNIFY_KEY=$(get_metadata "unify-key")
log "Initial unify-key: $([ -n "$PREV_UNIFY_KEY" ] && echo '(set)' || echo '(empty)')"

while true; do
    # Long-poll for metadata changes
    RESPONSE=$(curl -sf -H "$METADATA_HEADER" \
        "$METADATA_URL/instance/attributes/?recursive=true&wait_for_change=true&last_etag=$ETAG" \
        2>/dev/null || echo "")

    if [[ -z "$RESPONSE" ]]; then
        log "Metadata poll returned empty, retrying in 5s"
        sleep 5
        continue
    fi

    # Extract new etag from response headers (re-request with header capture)
    ETAG=$(curl -sf -H "$METADATA_HEADER" \
        -o /dev/null -D - \
        "$METADATA_URL/instance/attributes/?recursive=true" \
        2>/dev/null | grep -i "etag:" | tr -d '\r' | awk '{print $2}' || echo "")

    # Check unify-key
    CURRENT_UNIFY_KEY=$(get_metadata "unify-key")

    if [[ -n "$CURRENT_UNIFY_KEY" && -z "$PREV_UNIFY_KEY" ]]; then
        do_assign "$CURRENT_UNIFY_KEY"
    elif [[ -z "$CURRENT_UNIFY_KEY" && -n "$PREV_UNIFY_KEY" ]]; then
        do_release
    fi

    PREV_UNIFY_KEY="$CURRENT_UNIFY_KEY"
done
