#!/usr/bin/env bash
# =============================================================================
# update-metadata.sh - Update metadata on all Droid pool VMs
# =============================================================================
#
# Discovers all pool VMs (droid-pool-ubuntu-*, droid-pool-windows-*) and
# updates their metadata. Automatically applies the correct startup script
# based on VM type.
#
# Usage:
#   ./update-metadata.sh [options]
#
# Options:
#   --update-startup-script    Push latest local startup script to all VMs
#   --update-pool-watcher      Push latest local pool watcher script to all VMs
#   --update-supervisord-conf  Push latest local supervisord.conf to Ubuntu VMs
#   --metadata KEY=VALUE       Add/update a metadata key (repeatable)
#   --ubuntu                   Target only Ubuntu pool VMs
#   --windows                  Target only Windows pool VMs
#   --vm-number N              Target a single VM by number (e.g. --vm-number 13)
#   --restart                  Stop+start VMs after update (only RUNNING VMs)
#   --env ENV                  Target environment: production or staging
#   --dry-run                  Show what would happen without making changes
#   -h, --help                 Show this help
#
# Examples:
#   # Push latest startup scripts to all production pool VMs
#   ./update-metadata.sh --update-startup-script
#
#   # Push latest pool watcher scripts
#   ./update-metadata.sh --update-pool-watcher
#
#   # Push both startup and pool watcher scripts
#   ./update-metadata.sh --update-startup-script --update-pool-watcher
#
#   # Push latest supervisord.conf to Ubuntu VMs
#   ./update-metadata.sh --update-supervisord-conf --ubuntu
#
#   # Update orchestra URL on all pool VMs and restart them
#   ./update-metadata.sh --metadata orchestra-url=https://new.api.url --restart
#
#   # Preview what would happen on staging
#   ./update-metadata.sh --update-startup-script --env staging --dry-run
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT="${GCP_PROJECT_ID:-gcp-project-vms}"
ZONE="${GCP_ZONE:-us-central1-f}"

# Startup script paths (local files)
UBUNTU_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/ubuntu-vm-startup.sh"
WINDOWS_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/windows-vm-startup.ps1"

# Pool watcher script paths (local files)
UBUNTU_POOL_WATCHER="$REPO_ROOT/communication/infra/scripts/droid-pool-watcher.sh"
WINDOWS_POOL_WATCHER="$REPO_ROOT/communication/infra/scripts/droid-pool-watcher.ps1"

# Supervisord config path (Ubuntu only)
UBUNTU_SUPERVISORD_CONF="$REPO_ROOT/communication/infra/scripts/ubuntu-vm-custom-image/packer/files/supervisord.conf"

# Options
UPDATE_STARTUP=false
UPDATE_POOL_WATCHER=false
UPDATE_SUPERVISORD_CONF=false
METADATA_ARGS=()
ONLY_UBUNTU=false
ONLY_WINDOWS=false
VM_NUMBER=""
RESTART=false
DRY_RUN=false
TARGET_ENV="production"

# =============================================================================
# Functions
# =============================================================================

usage() {
    sed -n '2,/^set /{ /^#/s/^# \{0,1\}//p; }' "$0"
}

die() { echo "ERROR: $*" >&2; exit 1; }

get_vm_type() {
    local name="$1"
    if [[ "$name" == droid-pool-ubuntu-* ]]; then
        echo "ubuntu"
    elif [[ "$name" == droid-pool-windows-* ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

get_vm_env() {
    if [[ "$1" == *-staging ]]; then
        echo "staging"
    else
        echo "production"
    fi
}

# =============================================================================
# Argument Parsing
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update-startup-script)    UPDATE_STARTUP=true; shift ;;
        --update-pool-watcher)      UPDATE_POOL_WATCHER=true; shift ;;
        --update-supervisord-conf)  UPDATE_SUPERVISORD_CONF=true; shift ;;
        --metadata)              METADATA_ARGS+=("$2"); shift 2 ;;
        --ubuntu)                ONLY_UBUNTU=true; shift ;;
        --windows)               ONLY_WINDOWS=true; shift ;;
        --vm-number)             VM_NUMBER="$2"; shift 2 ;;
        --restart)               RESTART=true; shift ;;
        --env)
            TARGET_ENV="$2"
            case "$TARGET_ENV" in
                production) ZONE="${GCP_ZONE:-us-central1-f}" ;;
                staging) ZONE="${GCP_ZONE:-us-central1-a}" ;;
                *) die "Invalid --env value: $TARGET_ENV (expected production or staging)" ;;
            esac
            shift 2
            ;;
        --dry-run)               DRY_RUN=true; shift ;;
        -h|--help)               usage; exit 0 ;;
        *)                       die "Unknown argument: $1" ;;
    esac
done

# Validate: at least one action
if [[ "$UPDATE_STARTUP" == false && "$UPDATE_POOL_WATCHER" == false && "$UPDATE_SUPERVISORD_CONF" == false && ${#METADATA_ARGS[@]} -eq 0 ]]; then
    die "Nothing to do. Specify --update-startup-script, --update-pool-watcher, --update-supervisord-conf, and/or --metadata KEY=VALUE"
fi

if [[ "$ONLY_UBUNTU" == true && "$ONLY_WINDOWS" == true ]]; then
    die "--ubuntu and --windows are mutually exclusive"
fi

command -v gcloud &>/dev/null || die "gcloud not found"

# Validate script files exist
if [[ "$UPDATE_STARTUP" == true ]]; then
    [[ -f "$UBUNTU_STARTUP_SCRIPT" ]] || die "Ubuntu startup script not found: $UBUNTU_STARTUP_SCRIPT"
    [[ -f "$WINDOWS_STARTUP_SCRIPT" ]] || die "Windows startup script not found: $WINDOWS_STARTUP_SCRIPT"
fi
if [[ "$UPDATE_POOL_WATCHER" == true ]]; then
    [[ -f "$UBUNTU_POOL_WATCHER" ]] || die "Ubuntu pool watcher not found: $UBUNTU_POOL_WATCHER"
    [[ -f "$WINDOWS_POOL_WATCHER" ]] || die "Windows pool watcher not found: $WINDOWS_POOL_WATCHER"
fi

# =============================================================================
# Discover VMs
# =============================================================================

echo "=========================================="
echo "  Update Pool VM Metadata"
echo "=========================================="
echo ""
if [[ "$DRY_RUN" == true ]]; then
    echo "  *** DRY RUN — no changes will be made ***"
    echo ""
fi
VM_TYPE_LABEL="all"
if [[ -n "$VM_NUMBER" ]]; then
    ENV_SUFFIX=""
    [[ "$TARGET_ENV" == "staging" ]] && ENV_SUFFIX="-staging"
    if [[ "$ONLY_UBUNTU" == true ]]; then
        VM_NAME_FILTER="name=droid-pool-ubuntu-${VM_NUMBER}${ENV_SUFFIX}"
        VM_TYPE_LABEL="ubuntu #${VM_NUMBER}"
    elif [[ "$ONLY_WINDOWS" == true ]]; then
        VM_NAME_FILTER="name=droid-pool-windows-${VM_NUMBER}${ENV_SUFFIX}"
        VM_TYPE_LABEL="windows #${VM_NUMBER}"
    else
        VM_NAME_FILTER="name~'^droid-pool-(ubuntu|windows)-${VM_NUMBER}${ENV_SUFFIX}$'"
        VM_TYPE_LABEL="#${VM_NUMBER} (any type)"
    fi
elif [[ "$ONLY_UBUNTU" == true ]]; then
    VM_TYPE_LABEL="ubuntu only"
    VM_NAME_FILTER="name~'^droid-pool-ubuntu-'"
elif [[ "$ONLY_WINDOWS" == true ]]; then
    VM_TYPE_LABEL="windows only"
    VM_NAME_FILTER="name~'^droid-pool-windows-'"
else
    VM_NAME_FILTER="name~'^droid-pool-(ubuntu|windows)-'"
fi

echo "  Project:  $PROJECT"
echo "  Zone:     $ZONE"
echo "  Target:   $TARGET_ENV"
echo "  VM type:  $VM_TYPE_LABEL"
echo ""

echo "Discovering pool VMs..."
VM_LIST=$(gcloud compute instances list \
    --project="$PROJECT" \
    --zones="$ZONE" \
    --filter="$VM_NAME_FILTER" \
    --format="csv[no-heading](name,status)" \
    2>/dev/null) || true

if [[ -z "$VM_LIST" ]]; then
    echo "No pool VMs found."
    exit 0
fi

# =============================================================================
# Filter and categorize VMs
# =============================================================================

declare -a TARGET_VMS=()
declare -A VM_TYPES=()
declare -A VM_STATUSES=()

echo ""
printf "  %-40s %-10s %-12s %s\n" "VM NAME" "TYPE" "STATUS" ""
printf "  %-40s %-10s %-12s %s\n" "-------" "----" "------" ""

while IFS=',' read -r vm_name vm_status; do
    [[ -z "$vm_name" ]] && continue

    vm_type=$(get_vm_type "$vm_name")
    [[ "$vm_type" == "unknown" ]] && continue

    # Filter by target environment
    skip_reason=""
    vm_env=$(get_vm_env "$vm_name")
    if [[ "$vm_env" != "$TARGET_ENV" ]]; then
        skip_reason="(skip: $vm_env)"
    fi

    printf "  %-40s %-10s %-12s %s\n" "$vm_name" "$vm_type" "$vm_status" "$skip_reason"

    if [[ -z "$skip_reason" ]]; then
        TARGET_VMS+=("$vm_name")
        VM_TYPES["$vm_name"]="$vm_type"
        VM_STATUSES["$vm_name"]="$vm_status"
    fi
done <<< "$VM_LIST"

echo ""

if [[ ${#TARGET_VMS[@]} -eq 0 ]]; then
    echo "No VMs match the target environment."
    exit 0
fi

echo "Target VMs: ${#TARGET_VMS[@]}"
echo ""

# =============================================================================
# Build update summary
# =============================================================================

echo "Updates to apply:"
if [[ "$UPDATE_STARTUP" == true ]]; then
    ubuntu_lines=$(wc -l < "$UBUNTU_STARTUP_SCRIPT" | tr -d ' ')
    windows_lines=$(wc -l < "$WINDOWS_STARTUP_SCRIPT" | tr -d ' ')
    echo "  - Startup script (ubuntu: ${ubuntu_lines} lines, windows: ${windows_lines} lines)"
fi
if [[ "$UPDATE_POOL_WATCHER" == true ]]; then
    ubuntu_watcher_lines=$(wc -l < "$UBUNTU_POOL_WATCHER" | tr -d ' ')
    windows_watcher_lines=$(wc -l < "$WINDOWS_POOL_WATCHER" | tr -d ' ')
    echo "  - Pool watcher (ubuntu: ${ubuntu_watcher_lines} lines, windows: ${windows_watcher_lines} lines)"
fi
for meta in "${METADATA_ARGS[@]}"; do
    echo "  - Metadata: $meta"
done
if [[ "$RESTART" == true ]]; then
    echo "  - Restart RUNNING VMs after update"
fi
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "Dry run complete. No changes made."
    exit 0
fi

# =============================================================================
# Apply updates
# =============================================================================

success_count=0
fail_count=0

for vm_name in "${TARGET_VMS[@]}"; do
    vm_type="${VM_TYPES[$vm_name]}"
    vm_status="${VM_STATUSES[$vm_name]}"

    echo "--- $vm_name ($vm_type, $vm_status) ---"

    # Update startup script
    if [[ "$UPDATE_STARTUP" == true ]]; then
        if [[ "$vm_type" == "ubuntu" ]]; then
            script_key="startup-script"
            script_path="$UBUNTU_STARTUP_SCRIPT"
        else
            script_key="windows-startup-script-ps1"
            script_path="$WINDOWS_STARTUP_SCRIPT"
        fi

        echo "  Updating $script_key..."
        if gcloud compute instances add-metadata "$vm_name" \
            --project="$PROJECT" \
            --zone="$ZONE" \
            --metadata-from-file="$script_key=$script_path" \
            2>&1; then
            echo "  Startup script updated."
        else
            echo "  FAILED to update startup script." >&2
            ((fail_count++)) || true
            continue
        fi
    fi

    # Update pool watcher script
    if [[ "$UPDATE_POOL_WATCHER" == true ]]; then
        if [[ "$vm_type" == "ubuntu" ]]; then
            watcher_path="$UBUNTU_POOL_WATCHER"
        else
            watcher_path="$WINDOWS_POOL_WATCHER"
        fi

        echo "  Updating pool-watcher-script..."
        if gcloud compute instances add-metadata "$vm_name" \
            --project="$PROJECT" \
            --zone="$ZONE" \
            --metadata-from-file="pool-watcher-script=$watcher_path" \
            2>&1; then
            echo "  Pool watcher updated."
        else
            echo "  FAILED to update pool watcher." >&2
            ((fail_count++)) || true
            continue
        fi

        if [[ "$vm_type" == "windows" && "$vm_status" == "RUNNING" && "$RESTART" == false ]]; then
            echo "  NOTE: running Windows VMs load pool-watcher-script during startup."
            echo "        Rerun with --restart to apply the updated watcher immediately."
        fi
    fi

    # Update supervisord config (Ubuntu only)
    if [[ "$UPDATE_SUPERVISORD_CONF" == true && "$vm_type" == "ubuntu" ]]; then
        echo "  Updating supervisord-conf..."
        if gcloud compute instances add-metadata "$vm_name" \
            --project="$PROJECT" \
            --zone="$ZONE" \
            --metadata-from-file="supervisord-conf=$UBUNTU_SUPERVISORD_CONF" \
            2>&1; then
            echo "  Supervisord config updated."
        else
            echo "  FAILED to update supervisord config." >&2
            ((fail_count++)) || true
            continue
        fi
    fi

    # Update custom metadata
    if [[ ${#METADATA_ARGS[@]} -gt 0 ]]; then
        # Join metadata args with the ^::^ delimiter for safety
        metadata_str=""
        for meta in "${METADATA_ARGS[@]}"; do
            if [[ -n "$metadata_str" ]]; then
                metadata_str+="::$meta"
            else
                metadata_str="$meta"
            fi
        done
        metadata_str="^::^${metadata_str}"

        echo "  Updating metadata..."
        if gcloud compute instances add-metadata "$vm_name" \
            --project="$PROJECT" \
            --zone="$ZONE" \
            --metadata="$metadata_str" \
            2>&1; then
            echo "  Metadata updated."
        else
            echo "  FAILED to update metadata." >&2
            ((fail_count++)) || true
            continue
        fi
    fi

    # Restart if requested (only for RUNNING VMs)
    if [[ "$RESTART" == true && "$vm_status" == "RUNNING" ]]; then
        echo "  Restarting (stop + start)..."
        gcloud compute instances stop "$vm_name" \
            --project="$PROJECT" --zone="$ZONE" --quiet 2>&1
        gcloud compute instances start "$vm_name" \
            --project="$PROJECT" --zone="$ZONE" 2>&1
        echo "  Restarted."
    elif [[ "$RESTART" == true && "$vm_status" != "RUNNING" ]]; then
        echo "  Skipping restart ($vm_status)"
    fi

    ((success_count++)) || true
    echo ""
done

# =============================================================================
# Summary
# =============================================================================

echo "=========================================="
echo "  Update Complete"
echo "=========================================="
echo ""
echo "  Updated: $success_count"
echo "  Failed:  $fail_count"
if [[ "$RESTART" == true ]]; then
    echo ""
    echo "  Restarted VMs will run the new startup script on boot."
    echo "  Stopped VMs will pick up changes on next start."
fi
