"""
Windows VM Configuration

Centralized configuration for GCP Windows VM lifecycle management.
"""

import os

# =============================================================================
# GCP Project Configuration
# =============================================================================
# Project B: Where VMs and static IPs are created
VM_PROJECT_ID = "gcp-project-runtime"

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
DOMAIN_SUFFIX = "vm.unify.ai"  # Subdomain for Windows VMs

# =============================================================================
# VM Configuration
# =============================================================================
VM_MACHINE_TYPE = "e2-standard-4"  # 4 vCPU, 16GB RAM
VM_DISK_SIZE_GB = 100
VM_DISK_TYPE = "pd-ssd"
VM_IMAGE_FAMILY = "windows-2025"
VM_IMAGE_PROJECT = "windows-cloud"

# =============================================================================
# Networking
# =============================================================================
VM_NETWORK = "default"
VM_TAGS = ["unity-windows-vm", "https-server", "http-server"]

# =============================================================================
# Environment
# =============================================================================
STAGING = os.getenv("STAGING", "").lower() == "true"
ENV_SUFFIX = "-staging" if STAGING else ""

# =============================================================================
# Script Configuration
# =============================================================================
# Path to the init script (in the same package directory)
INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "windows-vm-startup.ps1",
)

# =============================================================================
# Secrets (loaded from Secret Manager in production)
# =============================================================================
MAK_KEY = os.getenv("MAK_KEY", "")
