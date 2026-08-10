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
#   hostname, github-token, orchestra-url, comms-url, unity-environment,
#   staging,
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

get_deploy_env() {
    local env_name
    env_name=$(get_metadata "unity-environment")
    if [[ -n "$env_name" ]]; then
        echo "$env_name"
    elif [[ -n "$(get_metadata "staging")" ]]; then
        echo "staging"
    else
        echo "production"
    fi
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

scrub_git_tokens() {
    for repo_dir in /magnitude /agent-service; do
        if [[ -d "$repo_dir/.git" ]]; then
            local url
            url=$(git -C "$repo_dir" remote get-url origin 2>/dev/null || true)
            if [[ "$url" == *"@github.com"* ]]; then
                git -C "$repo_dir" remote set-url origin "$(echo "$url" | sed 's|https://[^@]*@|https://|')" 2>/dev/null || true
            fi
        fi
    done
}

# =============================================================================
# Runtime dependencies not baked into image
# =============================================================================
apt-get update -qq
# DisplayHarness deps (pool images use --no-install-recommends, so these are
# not pulled in via xfce4-goodies the way the local desktop Docker image is):
#   xdotool  — mouse/keyboard
#   scrot    — screenshot fallback when xfce4-screenshooter is absent
#   wmctrl   — Chromium window discovery/focus
apt-get install -y --no-install-recommends xdotool scrot wmctrl

# =============================================================================
# Read Configuration
# =============================================================================
echo ""
echo "Reading GCP metadata..."
CONFIG_HOSTNAME=$(get_metadata "hostname")
GITHUB_TOKEN=$(get_metadata "github-token")
DEPLOY_ENV=$(get_deploy_env)
TLS_FULLCHAIN=$(get_metadata "tls-fullchain")
TLS_PRIVKEY=$(get_metadata "tls-privkey")

echo "  Hostname:       ${CONFIG_HOSTNAME:-(not configured)}"
echo "  GitHub Token:   ${GITHUB_TOKEN:+(set)}"
echo "  Deploy Env:     ${DEPLOY_ENV}"
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
# Update Supervisord Config from Metadata
# =============================================================================
SUPERVISORD_CONF_PATH="/etc/supervisor/conf.d/unity-vm.conf"
if curl -sf -H "$METADATA_HEADER" "$METADATA_URL/supervisord-conf" \
    -o "$SUPERVISORD_CONF_PATH" 2>/dev/null && [[ -s "$SUPERVISORD_CONF_PATH" ]]; then
    echo "Supervisord config updated from metadata"
else
    echo "No supervisord-conf metadata, using baked-in version"
fi

# =============================================================================
# Service User Hardening (converge on every boot)
# =============================================================================
setcap CAP_NET_BIND_SERVICE=+ep /usr/local/bin/caddy 2>/dev/null || true
echo "Caddy: CAP_NET_BIND_SERVICE set"

# Ensure unityuser cannot escalate to root
deluser unityuser sudo 2>/dev/null || true
rm -f /etc/sudoers.d/*unityuser* 2>/dev/null || true

# =============================================================================
# Desktop as unityuser: converge filesystem for non-root desktop
# =============================================================================
echo ""
echo "=== Converging desktop for unityuser ==="

# Make /root traversable so unityuser can reach Playwright browsers at /root/.cache/ms-playwright
chmod 711 /root
chmod 711 /root/.cache 2>/dev/null || true
echo "  /root made traversable (711)"

# Single source of truth for all VM-level environment variables.
# /etc/default/unity-vm is read by:
#   - this startup-script (source before launching supervisord)
#   - systemd supervisor.service (EnvironmentFile in drop-in below)
#   - interactive shells (/etc/profile.d/unity-vm.sh sources this)
# When adding new env vars, add them HERE and they propagate everywhere.
cat > /etc/default/unity-vm << 'EOF'
DISPLAY=:1
VNC_GEOMETRY=1920x1080
VNC_DEPTH=24
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
EOF
chmod 644 /etc/default/unity-vm
echo "  /etc/default/unity-vm written (canonical env vars)"

cat > /etc/profile.d/unity-vm.sh << 'PROFILE'
# Interactive shells: export all vars from the canonical env file.
set -a
. /etc/default/unity-vm
set +a
PROFILE
chmod +x /etc/profile.d/unity-vm.sh
echo "  /etc/profile.d/unity-vm.sh -> sources /etc/default/unity-vm"

# Systemd drop-in so supervisor.service auto-restarts inherit the env vars.
# Without this, systemd restarts supervisor without VNC_GEOMETRY/VNC_DEPTH,
# causing a config parse error and an infinite crash loop.
mkdir -p /etc/systemd/system/supervisor.service.d
cat > /etc/systemd/system/supervisor.service.d/unity-env.conf << 'DROPIN'
[Service]
EnvironmentFile=/etc/default/unity-vm
DROPIN
systemctl daemon-reload
echo "  systemd drop-in created for supervisor.service"

# XFCE config via system-wide XDG fallback (read by any user, not just root)
mkdir -p /etc/xdg/xfce4/xfconf/xfce-perchannel-xml

cat > /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
  </property>
</channel>
EOF

cat > /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
  </property>
</channel>
EOF

cat > /etc/xdg/xfce4/helpers.rc << 'EOF'
WebBrowser=chromium-browser
FileManager=thunar
TerminalEmulator=xfce4-terminal
EOF
echo "  XFCE config written to /etc/xdg/xfce4/"

# Pre-create writable dirs in /Unity for XFCE desktop session
# /Unity itself is root:root 755 (required by SSHD ChrootDirectory)
for dir in .config .local .cache; do
    mkdir -p "/Unity/$dir"
    chown unityuser:unityuser "/Unity/$dir"
done
# Symlink Playwright browser cache so $HOME/.cache/ms-playwright resolves to the actual install location
ln -sfn /root/.cache/ms-playwright /Unity/.cache/ms-playwright
echo "  /Unity/.config, .local, .cache created for unityuser"

# Shell config for unityuser desktop terminal sessions
cat > /Unity/.bashrc << 'BASHRC'
if [[ -d /Unity ]] && [[ $- == *i* ]] && [[ -n "$DISPLAY" ]] && [[ -z "$UNITY_SHELL_INIT" ]]; then
    export UNITY_SHELL_INIT=1
    cd /Unity
fi
BASHRC
chown unityuser:unityuser /Unity/.bashrc
echo "  /Unity/.bashrc configured"

echo "Desktop convergence complete"

# =============================================================================
# VNC Default Password (so supervisord can start TigerVNC)
# =============================================================================
mkdir -p /etc/vnc
VNC_PASSWORD="unify123" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
pw = (os.environ.get('VNC_PASSWORD', 'unify123') + '\x00' * 8)[:8].encode('latin-1')
with open('/etc/vnc/passwd', 'wb') as f:
    f.write(DES.new(key, DES.MODE_ECB).encrypt(pw))
os.chmod('/etc/vnc/passwd', 0o640)
os.system('chgrp unityuser /etc/vnc/passwd')
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
case "$DEPLOY_ENV" in
    staging) UNITY_BRANCH="staging" ;;
    *) UNITY_BRANCH="main" ;;
esac

# =============================================================================
# Update Magnitude
# =============================================================================
echo ""
echo "=== Updating Magnitude ==="

if [[ -d "/magnitude/.git" ]]; then
    cd /magnitude
    [[ -n "$GITHUB_TOKEN" ]] && git remote set-url origin "$MAGNITUDE_URL" 2>/dev/null || true
    git fetch --depth 1 origin main 2>&1 || true
    git reset --hard origin/main 2>&1 || true
    commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
    save_commit_hash /magnitude "$commit"
    echo "  Magnitude updated (commit: $commit)"
elif [[ ! -f "/magnitude/package.json" ]]; then
    echo "  Cloning Magnitude..."
    git clone --depth 1 --branch main "$MAGNITUDE_URL" /magnitude 2>&1
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

# agent-service's observationScaling.ts resolves the shared policy from
# /unify/common (and a few fallbacks). Pool VMs sparse-checkout only the
# service tree, so that file must be installed explicitly after clone.
OBS_SCALING_POLICY_PATH="/unify/common/observation_scaling_policy.json"

saved_hash=$(get_saved_commit_hash /agent-service)
remote_hash=$(get_remote_commit_hash "$UNITY_URL" "$UNITY_BRANCH")
needs_update=true

if [[ -f "/agent-service/package.json" && -n "$saved_hash" && -n "$remote_hash" && "$saved_hash" == "$remote_hash" && -f "$OBS_SCALING_POLICY_PATH" ]]; then
    echo "  Agent Service up-to-date (commit: $saved_hash)"
    needs_update=false
elif [[ -f "/agent-service/package.json" && -n "$saved_hash" && -n "$remote_hash" && "$saved_hash" == "$remote_hash" ]]; then
    echo "  Agent Service commit current but observation scaling policy missing; refreshing"
elif [[ -n "$saved_hash" && -n "$remote_hash" ]]; then
    echo "  Agent Service update available ($saved_hash -> $remote_hash)"
fi

if [[ "$needs_update" == "true" ]]; then
    tmp_dir=$(mktemp -d)
    git clone --depth 1 --branch "$UNITY_BRANCH" --filter=blob:none --sparse "$UNITY_URL" "$tmp_dir" 2>&1
    cd "$tmp_dir"
    git sparse-checkout set agent-service unify/common 2>&1
    commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")

    # Preserve node_modules to speed up npm install
    if [[ -d /agent-service/node_modules ]]; then
        mv /agent-service/node_modules "$tmp_dir/agent-service/node_modules"
    fi
    rm -rf /agent-service
    mv agent-service /agent-service
    mkdir -p /unify/common
    if [[ -f unify/common/observation_scaling_policy.json ]]; then
        cp unify/common/observation_scaling_policy.json "$OBS_SCALING_POLICY_PATH"
        echo "  Installed observation scaling policy at $OBS_SCALING_POLICY_PATH"
    else
        echo "  WARNING: observation_scaling_policy.json missing from sparse checkout"
    fi
    save_commit_hash /agent-service "$commit"
    rm -rf "$tmp_dir"
    echo "  Agent Service updated (commit: $commit)"
fi

echo "  Installing dependencies..."
cd /agent-service
npm install 2>&1

scrub_git_tokens

# =============================================================================
# Configure Caddy
# =============================================================================
echo ""
echo "=== Configuring Caddy ==="

mkdir -p /etc/caddy /var/log/caddy
chown unityuser:unityuser /var/log/caddy

TLS_DIRECTIVE=""
if [[ -n "$TLS_FULLCHAIN" && -n "$TLS_PRIVKEY" ]]; then
    mkdir -p /etc/caddy/certs
    echo "$TLS_FULLCHAIN" > /etc/caddy/certs/fullchain.pem
    echo "$TLS_PRIVKEY" > /etc/caddy/certs/privkey.pem
    chgrp unityuser /etc/caddy/certs/privkey.pem
    chmod 640 /etc/caddy/certs/privkey.pem
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
# Enforce Firewall Rules (converge on every boot)
# =============================================================================
echo ""
echo "=== Enforcing firewall rules ==="

for chain in UNITY-INBOUND UNITY-OUTBOUND; do
    iptables -N $chain 2>/dev/null || iptables -F $chain
done
# Wire custom chains into main chains (idempotent)
iptables -C INPUT -j UNITY-INBOUND 2>/dev/null || iptables -I INPUT -j UNITY-INBOUND
iptables -C OUTPUT -j UNITY-OUTBOUND 2>/dev/null || iptables -I OUTPUT -j UNITY-OUTBOUND

# Inbound: block direct access to internal service ports (6080/3000 behind Caddy)
iptables -A UNITY-INBOUND -p tcp --dport 6080 ! -i lo -j DROP
iptables -A UNITY-INBOUND -p tcp --dport 3000 ! -i lo -j DROP

# Outbound: block metadata server for unityuser (prevents reading secrets/tokens)
iptables -A UNITY-OUTBOUND -d 169.254.169.254 -m owner --uid-owner unityuser -j DROP

echo "  Inbound: ports 6080/3000 blocked (behind Caddy)"
echo "  Outbound: metadata server blocked for unityuser"

# =============================================================================
# Start Services before marking idle so the desktop surface is reachable.
# Pool idle does not mean agent-service is running yet; the watcher starts
# agent-service only after assignment metadata appears.
# =============================================================================
ELAPSED=$(( $(date +%s) - START_TIME ))
echo ""
echo "Setup complete in ${ELAPSED}s - launching supervisord"

# Ensure service logs are writable by unityuser
touch /var/log/agent-service.log
chown unityuser:unityuser /var/log/agent-service.log

# Hand supervisor to systemd rather than running our own copy. Launching one
# here left TWO supervisords per VM -- systemd's (the Debian unit, which loads
# the same programs via supervisord.conf's `[include] conf.d/*.conf`) plus this
# one. Both bound :443 and both bound Caddy's admin :2019, so the reload in the
# pool watcher's configure_caddy_hostname reached only one of them: the other
# kept serving the pool vhost and answered assistant-host requests with an
# empty 200, which surfaced in the pod as a ContentTypeError from /api/act on
# whichever connection happened to land there.
#
# systemd is the intended owner -- supervisor.service.d/unity-env.conf exists
# precisely so its Restart=on-failure inherits these vars. Start it only now,
# after setup, because the unit is disabled at boot (see install-base.sh): its
# programs must not come up before the VNC password and Caddy hostname are
# written.
systemctl restart supervisor

echo "Waiting for Caddy on port 443..."
for i in $(seq 1 30); do
    if ss -tlnp | grep -q ':443 '; then
        echo "Caddy listening on port 443 (after ${i}s)"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "WARNING: Caddy not listening after 30s, marking idle anyway"
    fi
    sleep 1
done

# =============================================================================
# Mark pool VM as idle + wipe github-token (via Comms API with GCP identity token)
# =============================================================================
COMMS_URL=$(get_metadata "comms-url")
ID_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=unity-comms-vm&format=full" \
    2>/dev/null || true)

if [[ -n "$COMMS_URL" && -n "$ID_TOKEN" ]]; then
    curl -sf -X POST "$COMMS_URL/infra/vm/mark-idle" \
        -H "Authorization: Bearer $ID_TOKEN" \
        -H "Content-Type: application/json" >/dev/null 2>&1 \
        && echo "Pool VM marked as idle" \
        || echo "WARNING: failed to mark VM as idle via Comms API"

    curl -sf -X POST "$COMMS_URL/infra/vm/wipe-metadata-key" \
        -H "Authorization: Bearer $ID_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"key": "github-token"}' >/dev/null 2>&1 \
        && echo "Wiped github-token from metadata" \
        || echo "WARNING: failed to wipe github-token via Comms API"
fi

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "Startup complete in ${TOTAL_ELAPSED}s — VM is idle and ready for assignment"

# Exit instead of blocking on our own supervisord for the VM's lifetime.
# systemd keeps the services alive now, and google-startup-scripts.service can
# finally complete -- the old `wait` held it open forever. Nothing else needs
# this script resident: the pool watcher is its own unit.
