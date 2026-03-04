"""
Tunnel Configuration

Centralized configuration for the tunnel relay service.
The control plane (API) runs in Cloud Run (this app).
The data plane (rathole + Caddy) runs on a single shared GCE VM.
"""

import os

# =============================================================================
# Environment
# =============================================================================
STAGING = os.getenv("STAGING")
ENV_SUFFIX = "-staging" if STAGING else ""
TUNNEL_SUBDOMAIN = "staging.tunnel.unify.ai" if STAGING else "tunnel.unify.ai"

# =============================================================================
# GCP Project Configuration
# =============================================================================
TUNNEL_PROJECT_ID = "gcp-project-vms"
DNS_PROJECT_ID = "gcp-project-dns"

# =============================================================================
# Tunnel Server VM Configuration
# =============================================================================
TUNNEL_VM_NAME = "unity-tunnel-server"
TUNNEL_VM_ZONE = "us-central1-a"
TUNNEL_VM_REGION = "us-central1"
TUNNEL_VM_MACHINE_TYPE = "e2-small"  # Shared across all clients
TUNNEL_VM_IMAGE_FAMILY = "ubuntu-2404-lts-amd64"
TUNNEL_VM_IMAGE_PROJECT = "ubuntu-os-cloud"
TUNNEL_VM_DISK_SIZE_GB = 20
TUNNEL_VM_DISK_TYPE = "pd-ssd"
TUNNEL_VM_NETWORK = "default"
TUNNEL_VM_TAGS = ["unity-tunnel-server", "https-server", "http-server", "allow-tunnel"]
TUNNEL_STATIC_IP_NAME = "unity-tunnel-server-ip"

# =============================================================================
# DNS Configuration
# =============================================================================
DNS_ZONE_NAME = "unifyai"  # Existing Cloud DNS managed zone in gcp-project-dns
TUNNEL_DOMAIN = "tunnel.unify.ai"  # Wildcard *.tunnel.unify.ai → tunnel VM

# =============================================================================
# Tunnel Relay (rathole) Configuration
# =============================================================================
TUNNEL_CONTROL_PORT = 7000  # rathole server control port (clients connect here)
TUNNEL_PORT_RANGE_START = 10000  # Internal port range for tunnel services
TUNNEL_PORT_RANGE_END = 60000

# =============================================================================
# GCS State Storage
# =============================================================================
# Tunnel registry and rathole config stored in GCS (no Firestore dependency needed).
# Cloud Run writes these; the tunnel VM watches and hot-reloads.
TUNNEL_GCS_BUCKET = f"unity-tunnel-config{ENV_SUFFIX}"
TUNNEL_REGISTRY_BLOB = "registry.json"  # Active tunnel metadata (JSON)
TUNNEL_SERVER_CONFIG_BLOB = "server.toml"  # rathole server config
TUNNEL_PORT_MAP_BLOB = "port-map.json"  # tunnel_id → internal_port (for Caddy)

# =============================================================================
# Startup Script Path
# =============================================================================
TUNNEL_INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "tunnel-server-startup.sh",
)
