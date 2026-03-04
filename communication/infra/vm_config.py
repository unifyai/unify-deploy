"""
VM Configuration

Centralized configuration for GCP VM lifecycle management (Windows and Ubuntu).
"""

import os

# =============================================================================
# GCP Project Configuration
# =============================================================================
# Dedicated project for assistant VMs and static IPs (isolated from GKE cluster)
VM_PROJECT_ID = "gcp-project-vms"

# DNS Project: Where DNS zone is managed (gcp-project-dns)
DNS_PROJECT_ID = "gcp-project-dns"

# =============================================================================
# Region/Zone Configuration
# =============================================================================
REGION = "us-central1"
ZONE = "us-central1-a"

# =============================================================================
# DNS Configuration (Managed in Project A)
# =============================================================================
DNS_ZONE_NAME = "unifyai"  # Existing Cloud DNS managed zone
DOMAIN_SUFFIX = "vm.unify.ai"  # Subdomain for VMs

# =============================================================================
# Environment
# =============================================================================
STAGING = os.getenv("STAGING", "").lower() == "true"
ENV_SUFFIX = "-staging" if STAGING else ""

# =============================================================================
# Shared VM Configuration
# =============================================================================
VM_DISK_TYPE = "pd-ssd"
VM_NETWORK = "default"

# =============================================================================
# Windows VM Configuration
# =============================================================================
WINDOWS_VM_MACHINE_TYPE = "e2-standard-4"  # 4 vCPU, 16GB RAM
WINDOWS_VM_DISK_SIZE_GB = 100
WINDOWS_VM_IMAGE_FAMILY = "unity-windows-vm"
WINDOWS_VM_IMAGE_PROJECT = "gcp-project-vms"
WINDOWS_VM_TAGS = ["unity-windows-vm", "https-server", "http-server", "allow-2222"]

# Path to the Windows init script
WINDOWS_INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "windows-vm-startup.ps1",
)

# =============================================================================
# Ubuntu VM Configuration
# =============================================================================
UBUNTU_VM_MACHINE_TYPE = "e2-standard-2"  # 2 vCPU, 8GB RAM
UBUNTU_VM_DISK_SIZE_GB = 50
UBUNTU_VM_IMAGE_FAMILY = "unity-ubuntu-vm"
UBUNTU_VM_IMAGE_PROJECT = "gcp-project-vms"
UBUNTU_VM_TAGS = ["unity-ubuntu-vm", "https-server", "http-server", "allow-2222"]

# =============================================================================
# SSH File Sync Configuration
# =============================================================================
SSH_SYNC_PORT = 2222  # Dedicated port for file sync (avoids conflicts with default SSH)

# Path to the Ubuntu init script
UBUNTU_INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "ubuntu-vm-startup.sh",
)

# =============================================================================
# Wildcard TLS Certificate (Secret Manager)
# =============================================================================
VM_WILDCARD_CERT_SECRET = "VM_WILDCARD_FULLCHAIN"
VM_WILDCARD_KEY_SECRET = "VM_WILDCARD_PRIVKEY"

# =============================================================================
# Secrets (loaded from Secret Manager in production)
# =============================================================================
MAK_KEY = os.getenv("MAK_KEY", "")
