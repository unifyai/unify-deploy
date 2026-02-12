#!/usr/bin/env bash
# =============================================================================
# update-metadata.sh - Update metadata on all Unity VMs
# =============================================================================
#
# Discovers all Unity VMs (unity-ubuntu-*, unity-win-*) and updates their
# metadata. Automatically applies the correct startup script based on VM type.
#
# Usage:
#   ./update-metadata.sh [options]
#
# Options:
#   --update-startup-script   Push latest local startup script to all VMs
#   --metadata KEY=VALUE      Add/update a metadata key (repeatable)
#   --restart                 Stop+start VMs after update (only RUNNING VMs)
#   --staging                 Target staging VMs only (default: non-staging)
#   --dry-run                 Show what would happen without making changes
#   -h, --help                Show this help
#
# Examples:
#   # Push latest startup scripts to all production VMs
#   ./update-metadata.sh --update-startup-script
#
#   # Update orchestra URL on all VMs and restart them
#   ./update-metadata.sh --metadata orchestra-url=https://new.api.url --restart
#
#   # Preview what would happen on staging
#   ./update-metadata.sh --update-startup-script --staging --dry-run
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT="${GCP_PROJECT_ID:-gcp-project-runtime}"
ZONE="${GCP_ZONE:-us-central1-a}"

# Startup script paths (local files)
UBUNTU_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/ubuntu-vm-startup.sh"
WINDOWS_STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/windows-vm-startup.ps1"

# Options
UPDATE_STARTUP=false
METADATA_ARGS=()
RESTART=false
DRY_RUN=false
STAGING=false

# =============================================================================
# Functions
# =============================================================================

usage() {
    sed -n '2,/^set /{ /^#/s/^# \{0,1\}//p; }' "$0"
}

die() { echo "ERROR: $*" >&2; exit 1; }

get_vm_type() {
    local name="$1"
    if [[ "$name" == unity-ubuntu-* ]]; then
        echo "ubuntu"
    elif [[ "$name" == unity-win-* ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

is_staging_vm() {
    [[ "$1" == *-staging ]]
}

# =============================================================================
# Argument Parsing
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update-startup-script) UPDATE_STARTUP=true; shift ;;
        --metadata)              METADATA_ARGS+=("$2"); shift 2 ;;
        --restart)               RESTART=true; shift ;;
        --staging)               STAGING=true; shift ;;
        --dry-run)               DRY_RUN=true; shift ;;
        -h|--help)               usage; exit 0 ;;
        *)                       die "Unknown argument: $1" ;;
    esac
done

# Validate: at least one action
if [[ "$UPDATE_STARTUP" == false && ${#METADATA_ARGS[@]} -eq 0 ]]; then
    die "Nothing to do. Specify --update-startup-script and/or --metadata KEY=VALUE"
fi

command -v gcloud &>/dev/null || die "gcloud not found"

# Validate startup script files exist
if [[ "$UPDATE_STARTUP" == true ]]; then
    [[ -f "$UBUNTU_STARTUP_SCRIPT" ]] || die "Ubuntu startup script not found: $UBUNTU_STARTUP_SCRIPT"
    [[ -f "$WINDOWS_STARTUP_SCRIPT" ]] || die "Windows startup script not found: $WINDOWS_STARTUP_SCRIPT"
fi

# =============================================================================
# Discover VMs
# =============================================================================

echo "=========================================="
echo "  Update Unity VM Metadata"
echo "=========================================="
echo ""
if [[ "$DRY_RUN" == true ]]; then
    echo "  *** DRY RUN — no changes will be made ***"
    echo ""
fi
echo "  Project:  $PROJECT"
echo "  Zone:     $ZONE"
echo "  Target:   $(if $STAGING; then echo "staging"; else echo "production"; fi)"
echo ""

echo "Discovering Unity VMs..."
VM_LIST=$(gcloud compute instances list \
    --project="$PROJECT" \
    --zones="$ZONE" \
    --filter="name~'^unity-(ubuntu|win)-'" \
    --format="csv[no-heading](name,status)" \
    2>/dev/null) || true

if [[ -z "$VM_LIST" ]]; then
    echo "No Unity VMs found."
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

    # Filter by staging/production
    skip_reason=""
    if $STAGING && ! is_staging_vm "$vm_name"; then
        skip_reason="(skip: not staging)"
    elif ! $STAGING && is_staging_vm "$vm_name"; then
        skip_reason="(skip: staging)"
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
            ((fail_count++))
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
            ((fail_count++))
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

    ((success_count++))
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
