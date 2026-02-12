#!/usr/bin/env bash
# =============================================================================
# migrate-vm.sh - Migrate a Unity VM to a new custom image
# =============================================================================
#
# Recreates an existing VM from the latest image in its family while preserving
# all metadata (API keys, hostname, etc.) and optionally user files.
#
# The startup script is refreshed from the local repo (not carried over from
# the old VM), so the new VM gets both the latest image AND latest startup script.
#
# Usage:
#   ./migrate-vm.sh --assistant-id ID --vm-type ubuntu|windows [options]
#
# Required:
#   --assistant-id ID       Assistant ID (numeric string)
#   --vm-type TYPE          "ubuntu" or "windows"
#
# Options:
#   --preserve-files        Keep old disk, copy user files to new VM (Ubuntu only)
#   --dry-run               Show what would happen without making changes
#   --yes                   Skip confirmation prompt
#   --staging               Target staging VM
#   -h, --help              Show this help
#
# What happens:
#   1. Reads all metadata from the existing VM
#   2. Stops and deletes the old VM
#   3. Creates a new VM from the latest image with same config
#   4. (--preserve-files) Attaches old disk, copies user files, cleans up
#
# The static IP and DNS record are untouched — they persist across migration.
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT="${GCP_PROJECT_ID:-gcp-project-runtime}"
ZONE="${GCP_ZONE:-us-central1-a}"
NETWORK="default"
DISK_TYPE="pd-ssd"

# Image families (must match vm_config.py)
UBUNTU_IMAGE_FAMILY="unity-ubuntu-vm"
WINDOWS_IMAGE_FAMILY="unity-windows-vm"

# Local startup scripts (latest versions)
UBUNTU_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/ubuntu-vm-startup.sh"
WINDOWS_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/windows-vm-startup.ps1"

# Paths to preserve on Ubuntu (rsync from old disk)
UBUNTU_PRESERVE_PATHS="/root/"
UBUNTU_EXCLUDE_PATTERNS=(
    ".vnc"
    ".config/xfce4"
    ".cache"
    ".bun"
    ".local"
    ".npm"
    ".node_modules"
)

# Options
ASSISTANT_ID=""
VM_TYPE=""
PRESERVE_FILES=false
DRY_RUN=false
YES=false
STAGING=false

# =============================================================================
# Functions
# =============================================================================

usage() {
    sed -n '2,/^set /{ /^#/s/^# \{0,1\}//p; }' "$0"
}

die() { echo "ERROR: $*" >&2; exit 1; }

confirm() {
    if [[ "$YES" == true ]]; then return 0; fi
    echo ""
    read -rp "$1 [y/N] " response
    [[ "$response" =~ ^[Yy]$ ]]
}

# Derive VM name from assistant ID (mirrors vm_helpers.py get_vm_name)
get_vm_name() {
    local id="$1" type="$2"
    local sanitized="${id,,}"          # lowercase
    sanitized="${sanitized//_/-}"      # replace _ with -
    local suffix=""
    if [[ "$STAGING" == true ]]; then suffix="-staging"; fi

    if [[ "$type" == "ubuntu" ]]; then
        echo "unity-ubuntu-${sanitized}${suffix}"
    else
        echo "unity-win-${sanitized}${suffix}"
    fi
}

# Extract a metadata value from the instance JSON
get_metadata_value() {
    local json="$1" key="$2"
    echo "$json" | jq -r \
        --arg k "$key" \
        '.metadata.items // [] | map(select(.key == $k)) | first // empty | .value // empty'
}

# Get all metadata keys (excluding startup script)
get_metadata_keys() {
    local json="$1" startup_key="$2"
    echo "$json" | jq -r \
        --arg sk "$startup_key" \
        '.metadata.items // [] | map(select(.key != $sk)) | .[].key'
}

# =============================================================================
# Argument Parsing
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --assistant-id)    ASSISTANT_ID="$2"; shift 2 ;;
        --vm-type)         VM_TYPE="$2"; shift 2 ;;
        --preserve-files)  PRESERVE_FILES=true; shift ;;
        --dry-run)         DRY_RUN=true; shift ;;
        --yes)             YES=true; shift ;;
        --staging)         STAGING=true; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "Unknown argument: $1" ;;
    esac
done

[[ -n "$ASSISTANT_ID" ]] || die "--assistant-id is required"
[[ "$VM_TYPE" == "ubuntu" || "$VM_TYPE" == "windows" ]] || die "--vm-type must be 'ubuntu' or 'windows'"
command -v gcloud &>/dev/null || die "gcloud not found"
command -v jq &>/dev/null || die "jq not found (brew install jq)"

if [[ "$PRESERVE_FILES" == true && "$VM_TYPE" == "windows" ]]; then
    die "--preserve-files is only supported for Ubuntu VMs (Windows lacks SSH for automated file copy)"
fi

# Resolve type-specific config
VM_NAME=$(get_vm_name "$ASSISTANT_ID" "$VM_TYPE")

if [[ "$VM_TYPE" == "ubuntu" ]]; then
    IMAGE_FAMILY="$UBUNTU_IMAGE_FAMILY"
    STARTUP_KEY="startup-script"
    LOCAL_STARTUP_SCRIPT="$UBUNTU_STARTUP_SCRIPT"
else
    IMAGE_FAMILY="$WINDOWS_IMAGE_FAMILY"
    STARTUP_KEY="windows-startup-script-ps1"
    LOCAL_STARTUP_SCRIPT="$WINDOWS_STARTUP_SCRIPT"
fi

[[ -f "$LOCAL_STARTUP_SCRIPT" ]] || die "Startup script not found: $LOCAL_STARTUP_SCRIPT"

# =============================================================================
# Phase 1: Describe old VM
# =============================================================================

echo "=========================================="
echo "  Migrate VM to New Image"
echo "=========================================="
echo ""
if [[ "$DRY_RUN" == true ]]; then
    echo "  *** DRY RUN — no changes will be made ***"
    echo ""
fi
echo "  Assistant:      $ASSISTANT_ID"
echo "  VM Name:        $VM_NAME"
echo "  VM Type:        $VM_TYPE"
echo "  Image Family:   $IMAGE_FAMILY"
echo "  Preserve Files: $PRESERVE_FILES"
echo ""

echo "=== Phase 1: Reading old VM configuration ==="

INSTANCE_JSON=$(gcloud compute instances describe "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --format=json 2>/dev/null) || die "VM not found: $VM_NAME"

# Extract instance properties
MACHINE_TYPE=$(echo "$INSTANCE_JSON" | jq -r '.machineType' | awk -F/ '{print $NF}')
DISK_SIZE_GB=$(echo "$INSTANCE_JSON" | jq -r '.disks[0].diskSizeGb')
DISK_NAME=$(echo "$INSTANCE_JSON" | jq -r '.disks[0].source' | awk -F/ '{print $NF}')
EXTERNAL_IP=$(echo "$INSTANCE_JSON" | jq -r '.networkInterfaces[0].accessConfigs[0].natIP // empty')
VM_STATUS=$(echo "$INSTANCE_JSON" | jq -r '.status')
HAS_DISPLAY=$(echo "$INSTANCE_JSON" | jq -r '.displayDevice.enableDisplay // false')

# Extract tags
TAGS=$(echo "$INSTANCE_JSON" | jq -r '(.tags.items // []) | join(",")')

# Extract labels
LABELS=$(echo "$INSTANCE_JSON" | jq -r '(.labels // {}) | to_entries | map("\(.key)=\(.value)") | join(",")')

echo "  Machine Type:  $MACHINE_TYPE"
echo "  Disk:          $DISK_NAME (${DISK_SIZE_GB}GB)"
echo "  External IP:   ${EXTERNAL_IP:-(none)}"
echo "  Status:        $VM_STATUS"
echo "  Tags:          ${TAGS:-(none)}"
echo "  Labels:        ${LABELS:-(none)}"
echo "  Display:       $HAS_DISPLAY"
echo ""

# Extract metadata (excluding startup script)
echo "  Metadata keys (preserved):"
METADATA_KEYS=$(get_metadata_keys "$INSTANCE_JSON" "$STARTUP_KEY")
if [[ -z "$METADATA_KEYS" ]]; then
    echo "    (none)"
else
    while IFS= read -r key; do
        echo "    - $key"
    done <<< "$METADATA_KEYS"
fi
echo "  Startup script: $STARTUP_KEY (refreshed from local file)"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "=== Dry run summary ==="
    echo ""
    echo "Would:"
    echo "  1. Stop VM $VM_NAME"
    if [[ "$PRESERVE_FILES" == true ]]; then
        echo "  2. Set auto-delete=false on disk $DISK_NAME"
    fi
    echo "  3. Delete VM $VM_NAME"
    echo "  4. Create new VM $VM_NAME from image family $IMAGE_FAMILY"
    echo "     with same metadata, IP, tags, labels"
    if [[ "$PRESERVE_FILES" == true ]]; then
        echo "  5. Attach old disk $DISK_NAME, copy user files, detach and delete"
    fi
    echo ""
    echo "Dry run complete. No changes made."
    exit 0
fi

# Confirm
if ! confirm "This will DELETE and RECREATE $VM_NAME. Proceed?"; then
    echo "Aborted."
    exit 1
fi

# =============================================================================
# Write metadata to temp files (bulletproof handling of special characters)
# =============================================================================

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo ""
echo "=== Preparing metadata ==="

# Write startup script reference (from local file, not old VM)
METADATA_FROM_FILE_ARGS="$STARTUP_KEY=$LOCAL_STARTUP_SCRIPT"

# Write each non-startup metadata value to a temp file
while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    value=$(get_metadata_value "$INSTANCE_JSON" "$key")
    echo -n "$value" > "$TMPDIR/$key"
    METADATA_FROM_FILE_ARGS+=",$key=$TMPDIR/$key"
    echo "  Saved: $key (${#value} chars)"
done <<< "$METADATA_KEYS"

echo ""

# =============================================================================
# Phase 2: Preserve old disk (optional)
# =============================================================================

if [[ "$PRESERVE_FILES" == true ]]; then
    echo "=== Phase 2: Preserving old disk ==="
    echo "  Setting auto-delete=false on $DISK_NAME..."
    gcloud compute instances set-disk-auto-delete "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --disk="$DISK_NAME" \
        --no-auto-delete \
        --quiet
    echo "  Disk $DISK_NAME will survive VM deletion."
    echo ""
fi

# =============================================================================
# Phase 3: Delete old VM, create new VM
# =============================================================================

echo "=== Phase 3: Replacing VM ==="

# Stop if running
if [[ "$VM_STATUS" == "RUNNING" ]]; then
    echo "  Stopping $VM_NAME..."
    gcloud compute instances stop "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --quiet
    echo "  Stopped."
fi

# Delete old VM
echo "  Deleting $VM_NAME..."
gcloud compute instances delete "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --quiet
echo "  Deleted."
echo ""

# Build creation command
echo "  Creating new $VM_NAME from $IMAGE_FAMILY..."

CREATE_ARGS=(
    --project="$PROJECT"
    --zone="$ZONE"
    --machine-type="$MACHINE_TYPE"
    --image-family="$IMAGE_FAMILY"
    --image-project="$PROJECT"
    --boot-disk-size="${DISK_SIZE_GB}GB"
    --boot-disk-type="$DISK_TYPE"
    --boot-disk-auto-delete
    --network="$NETWORK"
    --metadata-from-file="$METADATA_FROM_FILE_ARGS"
    --maintenance-policy=MIGRATE
    --restart-on-failure
)

# Static IP
if [[ -n "$EXTERNAL_IP" ]]; then
    CREATE_ARGS+=(--address="$EXTERNAL_IP")
fi

# Tags
if [[ -n "$TAGS" ]]; then
    CREATE_ARGS+=(--tags="$TAGS")
fi

# Labels
if [[ -n "$LABELS" ]]; then
    CREATE_ARGS+=(--labels="$LABELS")
fi

# Display device (Windows)
if [[ "$HAS_DISPLAY" == "true" ]]; then
    CREATE_ARGS+=(--enable-display-device)
fi

gcloud compute instances create "$VM_NAME" "${CREATE_ARGS[@]}"

echo ""
echo "  VM $VM_NAME created from latest $IMAGE_FAMILY image."
echo ""

# =============================================================================
# Phase 4: File restoration (optional, Ubuntu only)
# =============================================================================

if [[ "$PRESERVE_FILES" == true ]]; then
    echo "=== Phase 4: Restoring user files ==="

    # Wait for VM to be ready
    echo "  Waiting for VM to boot..."
    sleep 30

    # Attach old disk as secondary
    echo "  Attaching old disk $DISK_NAME..."
    gcloud compute instances attach-disk "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --disk="$DISK_NAME" \
        --device-name="old-boot" \
        --quiet

    echo "  Waiting for disk to be available..."
    sleep 10

    # Build rsync exclude args
    EXCLUDE_ARGS=""
    for pattern in "${UBUNTU_EXCLUDE_PATTERNS[@]}"; do
        EXCLUDE_ARGS+="--exclude='$pattern' "
    done

    # SSH in and copy files
    echo "  Copying user files from old disk..."
    gcloud compute ssh "root@$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --quiet \
        --command="
            set -e
            echo 'Mounting old disk...'

            mkdir -p /mnt/old-root

            # Try common partition layouts
            if lsblk /dev/sdb1 &>/dev/null; then
                mount /dev/sdb1 /mnt/old-root
            elif lsblk /dev/sdb &>/dev/null; then
                mount /dev/sdb /mnt/old-root
            else
                echo 'ERROR: Could not find old disk partition'
                exit 1
            fi

            echo 'Copying files from /mnt/old-root/root/ to /root/...'
            rsync -av \
                $EXCLUDE_ARGS \
                /mnt/old-root/root/ /root/ \
                || echo 'WARNING: rsync had errors (some files may not have been copied)'

            echo 'Unmounting old disk...'
            umount /mnt/old-root
            rmdir /mnt/old-root
            echo 'File restoration complete.'
        " 2>&1

    echo ""

    # Detach and delete old disk
    echo "  Detaching old disk..."
    gcloud compute instances detach-disk "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --disk="$DISK_NAME" \
        --quiet

    echo "  Deleting old disk $DISK_NAME..."
    gcloud compute disks delete "$DISK_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --quiet

    echo "  Old disk cleaned up."
    echo ""
fi

# =============================================================================
# Summary
# =============================================================================

# Get the hostname from metadata
HOSTNAME=$(get_metadata_value "$INSTANCE_JSON" "hostname")

echo "=========================================="
echo "  Migration Complete"
echo "=========================================="
echo ""
echo "  VM:          $VM_NAME"
echo "  Image:       $IMAGE_FAMILY (latest)"
echo "  IP:          ${EXTERNAL_IP:-(ephemeral)}"
if [[ -n "$HOSTNAME" ]]; then
    echo "  Desktop URL: https://$HOSTNAME/desktop/"
fi
if [[ "$PRESERVE_FILES" == true ]]; then
    echo "  Files:       Restored from old disk"
else
    echo "  Files:       Fresh (repos will be re-cloned by startup script)"
fi
echo ""
echo "  The startup script is running — services will be ready shortly."
echo "  Check boot progress: gcloud compute ssh root@$VM_NAME --zone=$ZONE --command='journalctl -u google-startup-scripts -f'"
