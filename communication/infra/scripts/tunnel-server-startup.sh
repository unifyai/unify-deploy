#!/bin/bash
# =============================================================================
# Tunnel Server Startup Script
#
# Runs on GCE VM boot via startup-script metadata.
# Sets up the shared tunnel relay:
#   - rathole (server mode): accepts client tunnel connections on port 7000
#   - Caddy (wildcard TLS): routes *.tunnel.unify.ai → rathole internal ports
#   - Config watcher: polls GCS for config updates, hot-reloads services
#
# GCP Metadata Keys:
#   dns-project       - GCP project that owns the Cloud DNS zone (for ACME DNS-01)
#   gcs-bucket        - GCS bucket for tunnel config (default: unity-tunnel-config)
#   tunnel-domain     - Base tunnel domain (default: tunnel.unify.ai)
#   control-port      - rathole control port (default: 7000)
#
# GCS Blobs (written by Cloud Run control plane):
#   server.toml       - rathole server config (service definitions + tokens)
#   port-map.json     - tunnel_id → internal_port (Caddy routing)
#   registry.json     - full tunnel metadata (not used by this VM directly)
#
# Usage:
#   gcloud compute instances create unity-tunnel-server \
#     --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
#     --machine-type=e2-small --zone=us-central1-a \
#     --tags=unity-tunnel-server,https-server,http-server,allow-7000,allow-tunnel \
#     --labels=environment=production,owner=platform,project=unity,dataclassification=confidential,application=tunnel-server \
#     --metadata-from-file=startup-script=tunnel-server-startup.sh \
#     --metadata=dns-project=gcp-project-dns,gcs-bucket=unity-tunnel-config,tunnel-domain=tunnel.unify.ai
#
# The --labels flag carries the governance labels required by the Vanta GCE
# required-labels test; every instance must be created with them. For the
# staging server (unity-tunnel-server-staging) set environment=staging and
# keep the other four label values identical.
#
# Firewall (one-off, per project — prod and staging):
#   Rathole control (7000): allow-7000 tag, source 0.0.0.0/0 (client desktops
#   dial in from anywhere). HTTP tunnels: https-server/http-server + Caddy on 443.
#   Raw-TCP tunnels (SFTP band 61000-61999, see tunnel_config.SFTP_PORT_RANGE_*)
#   are dialled by the hosted runtime only, so they are opened to that egress
#   IP rather than the public internet (keeps the Vanta public-ports test green):
#
#   gcloud compute firewall-rules create allow-tunnel-sftp \
#     --allow=tcp:61000-61999 --target-tags=allow-tunnel \
#     --source-ranges=203.0.113.12/32   # unity-gke-egress-ip (Cloud NAT)
# =============================================================================

set -e

if [[ $EUID -ne 0 ]]; then
    echo "Re-executing as root..."
    exec sudo bash "$0" "$@"
fi

METADATA_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
METADATA_HEADER="Metadata-Flavor: Google"

START_TIME=$(date +%s)

echo "=========================================="
echo "  Tunnel Server Startup Script"
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
# Read Configuration
# =============================================================================
echo "Reading GCP metadata..."

DNS_PROJECT=$(get_metadata "dns-project")
GCS_BUCKET=$(get_metadata "gcs-bucket")
TUNNEL_DOMAIN=$(get_metadata "tunnel-domain")
CONTROL_PORT=$(get_metadata "control-port")

# Defaults
DNS_PROJECT=${DNS_PROJECT:-"gcp-project-dns"}
GCS_BUCKET=${GCS_BUCKET:-"unity-tunnel-config"}
TUNNEL_DOMAIN=${TUNNEL_DOMAIN:-"tunnel.unify.ai"}
CONTROL_PORT=${CONTROL_PORT:-"7000"}

echo ""
echo "Configuration:"
echo "  DNS Project:   $DNS_PROJECT"
echo "  GCS Bucket:    $GCS_BUCKET"
echo "  Tunnel Domain: $TUNNEL_DOMAIN"
echo "  Control Port:  $CONTROL_PORT"
echo ""

# =============================================================================
# Versions
# =============================================================================
RATHOLE_VERSION="0.5.0"
CADDY_VERSION="2.10.0"
GO_VERSION="1.23.4"

# =============================================================================
# Directories
# =============================================================================
TUNNEL_DIR="/opt/tunnel-server"
CONFIG_DIR="$TUNNEL_DIR/config"
STATIC_DIR="$TUNNEL_DIR/static"
LOG_DIR="/var/log/tunnel-server"

mkdir -p "$CONFIG_DIR" "$STATIC_DIR" "$LOG_DIR"

# =============================================================================
# Install System Dependencies
# =============================================================================
echo "=== Installing system dependencies ==="

export DEBIAN_FRONTEND=noninteractive

if ! command -v jq &>/dev/null || ! command -v unzip &>/dev/null || ! command -v gcloud &>/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends \
        jq curl wget tar unzip ca-certificates apt-transport-https gnupg

    # Install gcloud CLI if not present
    if ! command -v gcloud &>/dev/null; then
        echo "  Installing Google Cloud SDK..."
        echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
            > /etc/apt/sources.list.d/google-cloud-sdk.list
        curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
            | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
        apt-get update
        apt-get install -y google-cloud-cli
    fi
fi

echo "System dependencies ready"

# =============================================================================
# Install rathole
# =============================================================================
echo ""
echo "=== Installing rathole v${RATHOLE_VERSION} ==="

if [[ ! -f /usr/local/bin/rathole ]] || ! rathole --version 2>/dev/null | grep -q "$RATHOLE_VERSION"; then
    RATHOLE_URL="https://github.com/rapiz1/rathole/releases/download/v${RATHOLE_VERSION}/rathole-x86_64-unknown-linux-gnu.zip"
    RATHOLE_TMP=$(mktemp -d)
    curl -fsSL -o "$RATHOLE_TMP/rathole.zip" "$RATHOLE_URL"
    unzip -o "$RATHOLE_TMP/rathole.zip" -d "$RATHOLE_TMP"
    mv "$RATHOLE_TMP/rathole" /usr/local/bin/rathole
    chmod +x /usr/local/bin/rathole
    rm -rf "$RATHOLE_TMP"
    echo "rathole v${RATHOLE_VERSION} installed"
else
    echo "rathole v${RATHOLE_VERSION} already installed"
fi

# =============================================================================
# Install Caddy with Google Cloud DNS plugin
# =============================================================================
echo ""
echo "=== Installing Caddy v${CADDY_VERSION} with googleclouddns plugin ==="

# Caddy needs to be built with xcaddy to include the DNS plugin for
# wildcard certificate DNS-01 challenges against Google Cloud DNS.
if [[ ! -f /usr/local/bin/caddy-tunnel ]] || ! /usr/local/bin/caddy-tunnel version 2>/dev/null | grep -q "$CADDY_VERSION"; then
    # Install Go (needed for xcaddy)
    if ! command -v go &>/dev/null; then
        echo "  Installing Go ${GO_VERSION}..."
        curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -xzC /usr/local
        export PATH="/usr/local/go/bin:$PATH"
    fi
    export HOME="${HOME:-/root}"
    export GOPATH="${HOME}/go"
    export GOMODCACHE="${GOPATH}/pkg/mod"
    export PATH="/usr/local/go/bin:${GOPATH}/bin:$PATH"
    mkdir -p "$GOMODCACHE"

    # Install xcaddy
    echo "  Installing xcaddy..."
    go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest

    # Build Caddy with Google Cloud DNS plugin
    echo "  Building Caddy with googleclouddns plugin..."
    xcaddy build "v${CADDY_VERSION}" \
        --with github.com/caddy-dns/googleclouddns \
        --output /usr/local/bin/caddy-tunnel

    chmod +x /usr/local/bin/caddy-tunnel
    echo "Caddy v${CADDY_VERSION} + googleclouddns installed"
else
    echo "Caddy v${CADDY_VERSION} + googleclouddns already installed"
fi

# =============================================================================
# Pull Initial Config from GCS
# =============================================================================
echo ""
echo "=== Pulling initial config from GCS ==="

pull_config() {
    # Download server.toml and port-map.json from GCS.
    # Returns 0 if either file changed, 1 if both unchanged.
    local changed=1

    for blob in server.toml port-map.json; do
        local remote="gs://${GCS_BUCKET}/${blob}"
        local local_path="${CONFIG_DIR}/${blob}"
        local tmp_path="${CONFIG_DIR}/${blob}.tmp"

        if gsutil -q cp "$remote" "$tmp_path" 2>/dev/null; then
            if [[ ! -f "$local_path" ]] || ! cmp -s "$tmp_path" "$local_path"; then
                mv "$tmp_path" "$local_path"
                echo "  Updated: $blob"
                changed=0
            else
                rm -f "$tmp_path"
            fi
        else
            echo "  Not found in GCS: $blob (will use empty default)"
            # Create empty defaults so services can start
            if [[ ! -f "$local_path" ]]; then
                if [[ "$blob" == "server.toml" ]]; then
                    echo -e "[server]\nbind_addr = \"0.0.0.0:${CONTROL_PORT}\"\n\n[server.services]" > "$local_path"
                else
                    echo "{}" > "$local_path"
                fi
                changed=0
            fi
        fi
    done

    return $changed
}

pull_config || true
echo "Config pull complete"

# =============================================================================
# Generate Caddyfile from port-map.json
# =============================================================================
echo ""
echo "=== Generating Caddyfile ==="

generate_caddyfile() {
    local port_map="${CONFIG_DIR}/port-map.json"
    local caddyfile="/etc/caddy/Caddyfile.tunnel"

    mkdir -p /etc/caddy

    # Start with wildcard block for TLS + routing
    cat > "$caddyfile" << CADDYEOF
# Auto-generated from port-map.json — do not edit manually.

*.${TUNNEL_DOMAIN}, ${TUNNEL_DOMAIN} {
    tls {
        dns googleclouddns {
            gcp_project ${DNS_PROJECT}
        }
    }

CADDYEOF

    # Add per-tunnel routing from port-map.json
    if [[ -f "$port_map" ]] && [[ "$(cat "$port_map")" != "{}" ]]; then
        jq -r 'to_entries[] | "\(.key) \(.value)"' "$port_map" | while read -r tid port; do
            cat >> "$caddyfile" << ROUTEEOF
    @${tid} host ${tid}.${TUNNEL_DOMAIN}
    handle @${tid} {
        reverse_proxy localhost:${port}
    }

ROUTEEOF
        done
    fi

    # Serve install.sh and static files on the bare domain
    cat >> "$caddyfile" << CADDYEOF
    # Bare domain: serve install script and health endpoint
    @root host ${TUNNEL_DOMAIN}
    handle @root {
        handle /health {
            respond "ok" 200
        }
        root * ${STATIC_DIR}
        file_server
    }

    # Unmatched subdomains
    handle {
        respond "Tunnel not found" 404
    }

    log {
        output file ${LOG_DIR}/caddy-access.log {
            roll_size 10mb
            roll_keep 5
        }
        format console
    }
}
CADDYEOF

    echo "Caddyfile generated at $caddyfile"
}

generate_caddyfile

# =============================================================================
# Generate Client Install Script (served at https://${TUNNEL_DOMAIN}/install.sh)
# =============================================================================
echo ""
echo "=== Generating client install script ==="

cat > "${STATIC_DIR}/install.sh" << 'INSTALLEOF'
#!/bin/bash
# Unity Tunnel Client Installer
#
# Usage:
#   curl -sSL https://__TUNNEL_DOMAIN__/install.sh | bash -s -- \
#     --token "TOKEN" --tunnel-id "ID" --local-port 8080

set -e

RATHOLE_VERSION="0.5.0"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --token) TOKEN="$2"; shift 2 ;;
        --tunnel-id) TUNNEL_ID="$2"; shift 2 ;;
        --local-port) LOCAL_PORT="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$TOKEN" || -z "$TUNNEL_ID" || -z "$LOCAL_PORT" ]]; then
    echo "Usage: install.sh --token TOKEN --tunnel-id ID --local-port PORT"
    exit 1
fi

TUNNEL_DOMAIN="__TUNNEL_DOMAIN__"
CONTROL_PORT="__CONTROL_PORT__"

echo "=== Unity Tunnel Client ==="
echo "  Tunnel ID:  $TUNNEL_ID"
echo "  Local Port: $LOCAL_PORT"
echo "  URL:        https://${TUNNEL_ID}.${TUNNEL_DOMAIN}"
echo ""

# Detect OS and architecture, map to exact GitHub release asset names.
# Available v0.5.0 assets:
#   rathole-x86_64-unknown-linux-gnu.zip
#   rathole-aarch64-unknown-linux-musl.zip
#   rathole-x86_64-apple-darwin.zip
#   rathole-x86_64-pc-windows-msvc.zip
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
    linux)
        case "$ARCH" in
            x86_64|amd64)  RATHOLE_TARGET="x86_64-unknown-linux-gnu" ;;
            aarch64|arm64) RATHOLE_TARGET="aarch64-unknown-linux-musl" ;;
            *)             echo "Unsupported architecture: $ARCH"; exit 1 ;;
        esac
        ;;
    darwin)
        # Only x86_64 binary published; runs on Apple Silicon via Rosetta 2
        RATHOLE_TARGET="x86_64-apple-darwin"
        ;;
    mingw*|msys*|cygwin*)
        echo "Windows detected (Git Bash / MSYS2)."
        echo "Use the PowerShell installer instead:"
        echo ""
        echo "  irm https://${TUNNEL_DOMAIN}/install.ps1 -OutFile install.ps1"
        echo "  .\\install.ps1 -Token \"TOKEN\" -TunnelId \"ID\" -LocalPort PORT"
        echo ""
        exit 1
        ;;
    *)
        echo "Unsupported OS: $OS"; exit 1
        ;;
esac

INSTALL_DIR="${HOME}/.unity-tunnel"
mkdir -p "$INSTALL_DIR"

# Download rathole if needed
RATHOLE_BIN="${INSTALL_DIR}/rathole"
if [[ ! -f "$RATHOLE_BIN" ]]; then
    echo "Downloading rathole v${RATHOLE_VERSION}..."
    DOWNLOAD_URL="https://github.com/rapiz1/rathole/releases/download/v${RATHOLE_VERSION}/rathole-${RATHOLE_TARGET}.zip"
    TMP_DIR=$(mktemp -d)
    curl -fsSL -o "${TMP_DIR}/rathole.zip" "$DOWNLOAD_URL"
    unzip -o "${TMP_DIR}/rathole.zip" -d "$TMP_DIR"
    mv "${TMP_DIR}/rathole" "$RATHOLE_BIN"
    chmod +x "$RATHOLE_BIN"
    rm -rf "$TMP_DIR"
    echo "rathole installed to $RATHOLE_BIN"
fi

# Write client config
CONFIG_FILE="${INSTALL_DIR}/${TUNNEL_ID}.toml"
cat > "$CONFIG_FILE" << EOF
[client]
remote_addr = "${TUNNEL_DOMAIN}:${CONTROL_PORT}"

[client.services.${TUNNEL_ID}]
token = "${TOKEN}"
local_addr = "127.0.0.1:${LOCAL_PORT}"
EOF

echo ""
echo "Config written to: $CONFIG_FILE"
echo ""
echo "Starting tunnel... (Ctrl+C to stop)"
echo "  Forwarding https://${TUNNEL_ID}.${TUNNEL_DOMAIN} → localhost:${LOCAL_PORT}"
echo ""

exec "$RATHOLE_BIN" "$CONFIG_FILE"
INSTALLEOF

# Substitute actual domain and port into the generated script
sed -i "s|__TUNNEL_DOMAIN__|${TUNNEL_DOMAIN}|g" "${STATIC_DIR}/install.sh"
sed -i "s|__CONTROL_PORT__|${CONTROL_PORT}|g" "${STATIC_DIR}/install.sh"

chmod +x "${STATIC_DIR}/install.sh"
echo "Client install script written to ${STATIC_DIR}/install.sh"

# =============================================================================
# Generate Windows Client Install Script (https://${TUNNEL_DOMAIN}/install.ps1)
# =============================================================================
echo ""
echo "=== Generating Windows client install script ==="

cat > "${STATIC_DIR}/install.ps1" << 'PS1EOF'
# Unity Tunnel Client Installer (Windows)
#
# Usage:
#   irm https://__TUNNEL_DOMAIN__/install.ps1 -OutFile install.ps1
#   .\install.ps1 -Token "TOKEN" -TunnelId "ID" -LocalPort 8080

param(
    [Parameter(Mandatory=$true)][string]$Token,
    [Parameter(Mandatory=$true)][string]$TunnelId,
    [Parameter(Mandatory=$true)][int]$LocalPort
)

$ErrorActionPreference = "Stop"

$RatholeVersion = "0.5.0"
$TunnelDomain = "__TUNNEL_DOMAIN__"
$ControlPort = "__CONTROL_PORT__"

Write-Host "=== Unity Tunnel Client ===" -ForegroundColor Cyan
Write-Host "  Tunnel ID:  $TunnelId"
Write-Host "  Local Port: $LocalPort"
Write-Host "  URL:        https://$TunnelId.$TunnelDomain"
Write-Host ""

# Detect architecture
$Arch = if ([Environment]::Is64BitOperatingSystem) { "x86_64" } else { "i686" }

$InstallDir = Join-Path $env:USERPROFILE ".unity-tunnel"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$RatholeBin = Join-Path $InstallDir "rathole.exe"

# Download rathole if needed
if (-not (Test-Path $RatholeBin)) {
    Write-Host "Downloading rathole v$RatholeVersion..."
    $DownloadUrl = "https://github.com/rapiz1/rathole/releases/download/v$RatholeVersion/rathole-$Arch-pc-windows-msvc.zip"
    $ZipPath = Join-Path $InstallDir "rathole.zip"

    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath
    Expand-Archive -Path $ZipPath -DestinationPath $InstallDir -Force
    Remove-Item $ZipPath

    if (-not (Test-Path $RatholeBin)) {
        Write-Error "Failed to extract rathole.exe"
        exit 1
    }
    Write-Host "rathole installed to $RatholeBin"
}

# Write client config
$ConfigFile = Join-Path $InstallDir "$TunnelId.toml"
@"
[client]
remote_addr = "${TunnelDomain}:${ControlPort}"

[client.services.$TunnelId]
token = "$Token"
local_addr = "127.0.0.1:$LocalPort"
"@ | Set-Content -Path $ConfigFile -Encoding UTF8

Write-Host ""
Write-Host "Config written to: $ConfigFile"
Write-Host ""
Write-Host "Starting tunnel... (Ctrl+C to stop)" -ForegroundColor Green
Write-Host "  Forwarding https://$TunnelId.$TunnelDomain -> localhost:$LocalPort"
Write-Host ""

& $RatholeBin $ConfigFile
PS1EOF

# Substitute actual domain and port into the generated script
sed -i "s|__TUNNEL_DOMAIN__|${TUNNEL_DOMAIN}|g" "${STATIC_DIR}/install.ps1"
sed -i "s|__CONTROL_PORT__|${CONTROL_PORT}|g" "${STATIC_DIR}/install.ps1"

echo "Windows install script written to ${STATIC_DIR}/install.ps1"

# =============================================================================
# Create Config Watcher Script
# =============================================================================
echo ""
echo "=== Creating config watcher ==="

cat > "${TUNNEL_DIR}/watch-config.sh" << WATCHEOF
#!/bin/bash
# Polls GCS for config changes and hot-reloads rathole + Caddy.

GCS_BUCKET="${GCS_BUCKET}"
CONFIG_DIR="${CONFIG_DIR}"
TUNNEL_DOMAIN="${TUNNEL_DOMAIN}"
DNS_PROJECT="${DNS_PROJECT}"
CONTROL_PORT="${CONTROL_PORT}"
STATIC_DIR="${STATIC_DIR}"
LOG_DIR="${LOG_DIR}"

POLL_INTERVAL=10

echo "Config watcher started (polling every \${POLL_INTERVAL}s)"

while true; do
    changed=false

    for blob in server.toml port-map.json; do
        remote="gs://\${GCS_BUCKET}/\${blob}"
        local_path="\${CONFIG_DIR}/\${blob}"
        tmp_path="\${CONFIG_DIR}/\${blob}.tmp"

        if gsutil -q cp "\$remote" "\$tmp_path" 2>/dev/null; then
            if [[ ! -f "\$local_path" ]] || ! cmp -s "\$tmp_path" "\$local_path"; then
                mv "\$tmp_path" "\$local_path"
                echo "[\$(date)] Updated: \$blob"
                changed=true
            else
                rm -f "\$tmp_path"
            fi
        fi
    done

    if [[ "\$changed" == "true" ]]; then
        echo "[\$(date)] Config changed, regenerating Caddyfile and reloading services..."

        # Regenerate Caddyfile
        port_map="\${CONFIG_DIR}/port-map.json"
        caddyfile="/etc/caddy/Caddyfile.tunnel"

        cat > "\$caddyfile" << CEOF
*.${TUNNEL_DOMAIN}, ${TUNNEL_DOMAIN} {
    tls {
        dns googleclouddns {
            gcp_project ${DNS_PROJECT}
        }
    }

CEOF

        if [[ -f "\$port_map" ]] && [[ "\$(cat "\$port_map")" != "{}" ]]; then
            jq -r 'to_entries[] | "\(.key) \(.value)"' "\$port_map" | while read -r tid port; do
                cat >> "\$caddyfile" << REOF
    @\${tid} host \${tid}.${TUNNEL_DOMAIN}
    handle @\${tid} {
        reverse_proxy localhost:\${port}
    }

REOF
            done
        fi

        cat >> "\$caddyfile" << CEOF
    @root host ${TUNNEL_DOMAIN}
    handle @root {
        handle /health {
            respond "ok" 200
        }
        root * ${STATIC_DIR}
        file_server
    }

    handle {
        respond "Tunnel not found" 404
    }

    log {
        output file ${LOG_DIR}/caddy-access.log {
            roll_size 10mb
            roll_keep 5
        }
        format console
    }
}
CEOF

        # Reload rathole (restart — rathole doesn't support graceful reload)
        systemctl restart rathole-server.service 2>/dev/null || true

        # Reload Caddy (graceful reload via API)
        /usr/local/bin/caddy-tunnel reload --config "\$caddyfile" --adapter caddyfile 2>/dev/null || \
            systemctl restart caddy-tunnel.service 2>/dev/null || true

        echo "[\$(date)] Services reloaded"
    fi

    sleep "\$POLL_INTERVAL"
done
WATCHEOF

chmod +x "${TUNNEL_DIR}/watch-config.sh"
echo "Config watcher created"

# =============================================================================
# Create systemd Services
# =============================================================================
echo ""
echo "=== Configuring systemd services ==="

# --- rathole server ---
cat > /etc/systemd/system/rathole-server.service << EOF
[Unit]
Description=rathole tunnel server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/rathole ${CONFIG_DIR}/server.toml
Restart=always
RestartSec=5
StandardOutput=append:${LOG_DIR}/rathole.log
StandardError=append:${LOG_DIR}/rathole.log

[Install]
WantedBy=multi-user.target
EOF

# --- Caddy (tunnel edition with DNS plugin) ---
cat > /etc/systemd/system/caddy-tunnel.service << EOF
[Unit]
Description=Caddy tunnel reverse proxy (wildcard TLS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/caddy-tunnel run --config /etc/caddy/Caddyfile.tunnel --adapter caddyfile
ExecReload=/usr/local/bin/caddy-tunnel reload --config /etc/caddy/Caddyfile.tunnel --adapter caddyfile
Restart=always
RestartSec=5
StandardOutput=append:${LOG_DIR}/caddy.log
StandardError=append:${LOG_DIR}/caddy.log
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

# --- Config watcher ---
cat > /etc/systemd/system/tunnel-config-watcher.service << EOF
[Unit]
Description=Tunnel config GCS watcher
After=network-online.target rathole-server.service caddy-tunnel.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash ${TUNNEL_DIR}/watch-config.sh
Restart=always
RestartSec=10
StandardOutput=append:${LOG_DIR}/watcher.log
StandardError=append:${LOG_DIR}/watcher.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# =============================================================================
# Enable and Start Services
# =============================================================================
echo ""
echo "=== Starting services ==="

systemctl enable --now rathole-server.service
echo "  rathole-server: started"

systemctl enable --now caddy-tunnel.service
echo "  caddy-tunnel: started"

systemctl enable --now tunnel-config-watcher.service
echo "  tunnel-config-watcher: started"

# =============================================================================
# Summary
# =============================================================================
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

echo ""
echo "=========================================="
echo "  Tunnel Server Ready!"
echo "=========================================="
echo ""
echo "Services:"
echo "  rathole server    : port ${CONTROL_PORT} (client connections)"
echo "  Caddy             : ports 80, 443 (HTTPS wildcard *.${TUNNEL_DOMAIN})"
echo "  Config watcher    : polling gs://${GCS_BUCKET}/ every 10s"
echo ""
echo "Endpoints:"
echo "  Health:           https://${TUNNEL_DOMAIN}/health"
echo "  Client installer: https://${TUNNEL_DOMAIN}/install.sh"
echo ""
echo "Logs:"
echo "  rathole:  ${LOG_DIR}/rathole.log"
echo "  Caddy:    ${LOG_DIR}/caddy.log"
echo "  Watcher:  ${LOG_DIR}/watcher.log"
echo "  Access:   ${LOG_DIR}/caddy-access.log"
echo ""
echo "VM IP: ${IP_ADDR}"
echo "Total startup time: ${ELAPSED} seconds"
echo ""
