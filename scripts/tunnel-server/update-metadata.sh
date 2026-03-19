#!/usr/bin/env bash
# =============================================================================
# update-metadata.sh - Update metadata on the tunnel server VM
# =============================================================================
#
# Updates the unity-tunnel-server VM metadata and/or startup script.
#
# Usage:
#   ./update-metadata.sh [options]
#
# Options:
#   --update-startup-script   Push latest local startup script to the VM
#   --metadata KEY=VALUE      Add/update a metadata key (repeatable)
#   --restart                 Stop+start the VM after update (only if RUNNING)
#   --env ENV                 Target environment: production, staging, or preview
#   --dry-run                 Show what would happen without making changes
#   -h, --help                Show this help
#
# Examples:
#   # Push latest startup script to production
#   ./update-metadata.sh --update-startup-script
#
#   # Push latest startup script to preview
#   ./update-metadata.sh --update-startup-script --env preview
#
#   # Update the GCS bucket metadata
#   ./update-metadata.sh --metadata gcs-bucket=unity-tunnel-config-staging
#
#   # Update startup script and restart to apply
#   ./update-metadata.sh --update-startup-script --restart
#
#   # Preview changes
#   ./update-metadata.sh --update-startup-script --dry-run
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT="${GCP_PROJECT_ID:-gcp-project-runtime}"
ZONE="${GCP_ZONE:-us-central1-a}"

STARTUP_SCRIPT="$REPO_ROOT/communication/infra/scripts/tunnel-server-startup.sh"

# Options
UPDATE_STARTUP=false
METADATA_ARGS=()
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

# =============================================================================
# Argument Parsing
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update-startup-script) UPDATE_STARTUP=true; shift ;;
        --metadata)              METADATA_ARGS+=("$2"); shift 2 ;;
        --restart)               RESTART=true; shift ;;
        --env)
            TARGET_ENV="$2"
            case "$TARGET_ENV" in
                production|staging|preview) ;;
                *) die "Invalid --env value: $TARGET_ENV (expected production, staging, or preview)" ;;
            esac
            shift 2
            ;;
        --dry-run)               DRY_RUN=true; shift ;;
        -h|--help)               usage; exit 0 ;;
        *)                       die "Unknown argument: $1" ;;
    esac
done

if [[ "$UPDATE_STARTUP" == false && ${#METADATA_ARGS[@]} -eq 0 ]]; then
    die "Nothing to do. Specify --update-startup-script and/or --metadata KEY=VALUE"
fi

# Resolve VM name based on environment
case "$TARGET_ENV" in
    staging) VM_NAME="unity-tunnel-server-staging" ;;
    preview) VM_NAME="unity-tunnel-server-preview" ;;
    *) VM_NAME="unity-tunnel-server" ;;
esac

command -v gcloud &>/dev/null || die "gcloud not found"

if [[ "$UPDATE_STARTUP" == true ]]; then
    [[ -f "$STARTUP_SCRIPT" ]] || die "Startup script not found: $STARTUP_SCRIPT"
fi

# =============================================================================
# Check VM Exists
# =============================================================================

echo "=========================================="
echo "  Update Tunnel Server Metadata"
echo "=========================================="
echo ""
if [[ "$DRY_RUN" == true ]]; then
    echo "  *** DRY RUN — no changes will be made ***"
    echo ""
fi
echo "  Project: $PROJECT"
echo "  Zone:    $ZONE"
echo "  VM:      $VM_NAME"
echo "  Target:  $TARGET_ENV"
echo ""

VM_STATUS=$(gcloud compute instances describe "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --format="value(status)" \
    2>/dev/null) || die "VM '$VM_NAME' not found in $PROJECT / $ZONE"

echo "  Status:  $VM_STATUS"
echo ""

# =============================================================================
# Build Update Summary
# =============================================================================

echo "Updates to apply:"
if [[ "$UPDATE_STARTUP" == true ]]; then
    script_lines=$(wc -l < "$STARTUP_SCRIPT" | tr -d ' ')
    echo "  - Startup script (${script_lines} lines)"
fi
for meta in "${METADATA_ARGS[@]}"; do
    echo "  - Metadata: $meta"
done
if [[ "$RESTART" == true ]]; then
    echo "  - Restart VM after update"
fi
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "Dry run complete. No changes made."
    exit 0
fi

# =============================================================================
# Apply Updates
# =============================================================================

# Update startup script
if [[ "$UPDATE_STARTUP" == true ]]; then
    echo "Updating startup-script..."
    gcloud compute instances add-metadata "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --metadata-from-file="startup-script=$STARTUP_SCRIPT" \
        2>&1
    echo "Startup script updated."
    echo ""
fi

# Update custom metadata
if [[ ${#METADATA_ARGS[@]} -gt 0 ]]; then
    metadata_str=""
    for meta in "${METADATA_ARGS[@]}"; do
        if [[ -n "$metadata_str" ]]; then
            metadata_str+="::$meta"
        else
            metadata_str="$meta"
        fi
    done
    metadata_str="^::^${metadata_str}"

    echo "Updating metadata..."
    gcloud compute instances add-metadata "$VM_NAME" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --metadata="$metadata_str" \
        2>&1
    echo "Metadata updated."
    echo ""
fi

# Restart if requested
if [[ "$RESTART" == true ]]; then
    if [[ "$VM_STATUS" == "RUNNING" ]]; then
        echo "Restarting VM (stop + start)..."
        gcloud compute instances stop "$VM_NAME" \
            --project="$PROJECT" --zone="$ZONE" --quiet 2>&1
        gcloud compute instances start "$VM_NAME" \
            --project="$PROJECT" --zone="$ZONE" 2>&1
        echo "VM restarted."
    else
        echo "Skipping restart (VM status: $VM_STATUS)"
    fi
    echo ""
fi

# =============================================================================
# Summary
# =============================================================================

echo "=========================================="
echo "  Update Complete"
echo "=========================================="
echo ""
if [[ "$RESTART" == true ]]; then
    echo "  The VM will run the new startup script on boot."
else
    echo "  Changes will take effect on next VM restart."
    echo "  Run with --restart to apply immediately."
fi
