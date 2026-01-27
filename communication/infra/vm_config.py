"""
VM Configuration

Centralized configuration for GCP VM lifecycle management (Windows and Ubuntu).
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
WINDOWS_VM_IMAGE_PROJECT = "gcp-project-runtime"
WINDOWS_VM_TAGS = ["unity-windows-vm", "https-server", "http-server"]

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
UBUNTU_VM_IMAGE_PROJECT = "gcp-project-runtime"
UBUNTU_VM_TAGS = ["unity-ubuntu-vm", "https-server", "http-server", "allow-6080"]

# Path to the Ubuntu init script
UBUNTU_INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "ubuntu-vm-startup.sh",
)

# =============================================================================
# Legacy aliases (for backward compatibility)
# =============================================================================
VM_MACHINE_TYPE = WINDOWS_VM_MACHINE_TYPE
VM_DISK_SIZE_GB = WINDOWS_VM_DISK_SIZE_GB
VM_IMAGE_FAMILY = WINDOWS_VM_IMAGE_FAMILY
VM_IMAGE_PROJECT = WINDOWS_VM_IMAGE_PROJECT
VM_TAGS = WINDOWS_VM_TAGS
INIT_SCRIPT_PATH = WINDOWS_INIT_SCRIPT_PATH

# =============================================================================
# Secrets (loaded from Secret Manager in production)
# =============================================================================
MAK_KEY = os.getenv("MAK_KEY", "")
