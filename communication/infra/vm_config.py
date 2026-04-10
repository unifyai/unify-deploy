"""
VM Configuration

Static constants for GCP VM lifecycle management (Windows and Ubuntu).
Environment-derived values (project IDs, zones, suffixes) live in
``common.settings.SETTINGS``.
"""

import os

# =============================================================================
# Shared VM Configuration
# =============================================================================
VM_DISK_TYPE = "pd-ssd"
VM_NETWORK = "default"

# =============================================================================
# DNS Configuration (Managed in gcp-project-dns project)
# =============================================================================
DNS_ZONE_NAME = "unifyai"  # Existing Cloud DNS managed zone
DOMAIN_SUFFIX = "vm.unify.ai"  # Subdomain for VMs

# =============================================================================
# Windows VM Configuration
# =============================================================================
WINDOWS_VM_MACHINE_TYPE = "e2-standard-4"  # 4 vCPU, 16GB RAM
WINDOWS_VM_DISK_SIZE_GB = 100
WINDOWS_VM_IMAGE_PROJECT = "gcp-project-vms"
WINDOWS_VM_TAGS = ["unity-windows-vm", "https-server", "allow-2222"]

# Path to the Windows init script
WINDOWS_INIT_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "windows-vm-startup.ps1",
)
WINDOWS_POOL_WATCHER_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "unity-pool-watcher.ps1",
)

# =============================================================================
# Ubuntu VM Configuration
# =============================================================================
UBUNTU_VM_MACHINE_TYPE = "e2-standard-2"  # 2 vCPU, 8GB RAM
UBUNTU_VM_DISK_SIZE_GB = 50
UBUNTU_VM_IMAGE_PROJECT = "gcp-project-vms"
UBUNTU_VM_TAGS = ["unity-ubuntu-vm", "https-server", "allow-2222"]

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
UBUNTU_POOL_WATCHER_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "unity-pool-watcher.sh",
)
UBUNTU_SUPERVISORD_CONF_PATH = os.path.join(
    os.path.dirname(__file__),
    "scripts",
    "ubuntu-vm-custom-image",
    "packer",
    "files",
    "supervisord.conf",
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

# =============================================================================
# VM Pool Configuration
# =============================================================================
POOL_SSH_USERNAME = "unityuser"
POOL_TARGET_IDLE = 5
POOL_TARGET_STOPPED = 3
POOL_BOOT_TIMEOUT_SECONDS = 300
POOL_RELEASE_TIMEOUT_SECONDS = 600
POOL_VM_CONTRACT_GENERATION = os.getenv(
    "POOL_VM_CONTRACT_GENERATION",
    "guest-contract-v3",
)
POOL_ASSISTANT_DISK_SIZE_GB = 64
POOL_ASSISTANT_DISK_TYPE = "pd-standard"
POOL_VM_NAME_PREFIX = "unity-pool"

SUPPORTED_POOL_VM_TYPES: tuple[str, ...] = ("ubuntu", "windows")

# Pool image families (separate from legacy to avoid affecting existing VMs)
POOL_UBUNTU_VM_IMAGE_FAMILY = "unity-pool-ubuntu-vm"
POOL_WINDOWS_VM_IMAGE_FAMILY = "unity-pool-windows-vm"
