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
POOL_TARGET_IDLE = 0
POOL_TARGET_STOPPED = 0
POOL_BOOT_TIMEOUT_SECONDS = 300
POOL_RELEASE_TIMEOUT_SECONDS = 600
POOL_VM_CONTRACT_GENERATION = os.getenv(
    "POOL_VM_CONTRACT_GENERATION",
    "guest-contract-v3",
)
POOL_ASSISTANT_DISK_SIZE_GB = 64
POOL_ASSISTANT_DISK_TYPE = "pd-standard"
POOL_VM_NAME_PREFIX = "unity-pool"
# Live Windows pool VMs/IPs/DNS also use unity-pool-* (same as Ubuntu).
WINDOWS_POOL_VM_NAME_PREFIX = "unity-pool"
# Historical prefixes that may still own reserved IPs/DNS after renames.
# Orphan reclaim must recognize these; new provisioning uses the live prefix.
POOL_VM_NAME_PREFIX_ALIASES: tuple[str, ...] = ("unity-pool", "droid-pool")
# Retired env suffixes with no controller of their own. Both prod and staging
# reclaimers may delete leftover network resources for these suffixes.
POOL_RETIRED_ENV_SUFFIXES: tuple[str, ...] = ("-preview",)


def pool_vm_name_prefix(vm_type: str) -> str:
    """Return the live pool VM name prefix for the given OS family."""
    return WINDOWS_POOL_VM_NAME_PREFIX if vm_type == "windows" else POOL_VM_NAME_PREFIX


def pool_vm_name_prefixes(vm_type: str) -> tuple[str, ...]:
    """Live prefix first, then historical aliases (deduped)."""
    live = pool_vm_name_prefix(vm_type)
    prefixes = [live]
    for alias in POOL_VM_NAME_PREFIX_ALIASES:
        if alias not in prefixes:
            prefixes.append(alias)
    return tuple(prefixes)


SUPPORTED_POOL_VM_TYPES: tuple[str, ...] = ("ubuntu", "windows")

# GCS bucket for archiving assistant filesystems between sessions
POOL_ASSISTANT_ARCHIVE_BUCKET = "unity-assistant-archives"

# Idle-disk garbage collection. An unattached per-assistant PD whose
# assistant still exists in Orchestra is eligible for deletion once
# detached for longer than this threshold, provided a GCS archive exists
# and is at least as fresh as the disk's last detach.
POOL_ASSISTANT_DISK_IDLE_HOURS = 24

# Hard cap for orphaned disks that have neither a fresh archive nor a
# matching live assistant. ``0`` disables the escape hatch; any positive
# value triggers deletion with a WARN log and an accepted-data-loss tag.
POOL_ASSISTANT_DISK_HARD_CAP_HOURS = 0

# Allow for minor skew between the GCE ``last_detach_timestamp`` and the
# GCS blob ``updated`` timestamp when deciding whether an archive is a
# superset of the PD state. Roughly the release-upload latency plus clock
# drift headroom.
POOL_ASSISTANT_DISK_ARCHIVE_FRESHNESS_SKEW_SECONDS = 300

# Pool image families (separate from legacy to avoid affecting existing VMs)
POOL_UBUNTU_VM_IMAGE_FAMILY = "unity-pool-ubuntu-vm"
# Windows pool is not yet migrated; live Windows VMs use the unity image family.
POOL_WINDOWS_VM_IMAGE_FAMILY = "unity-pool-windows-vm"

# Governance labels required by the Vanta "GCE instances have required labels"
# test. Applied to every pool VM at creation so new VMs are born compliant.
# dataclassification is confidential because pool VMs run live customer
# assistant sessions — a system takes the highest classification of data it
# can process. The environment label is resolved at runtime from the deploy
# env and is therefore not part of this static set.
POOL_GOVERNANCE_LABELS = {
    "owner": "platform",
    "project": "unity",
    "dataclassification": "confidential",
    "application": "assistant-desktop",
}
