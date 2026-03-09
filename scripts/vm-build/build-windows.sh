#!/usr/bin/env bash
# =============================================================================
# build-windows.sh - Build Unity Pool Windows VM image via Packer
# =============================================================================
#
# Builds the pool Windows image (base + pool overlay) into the
# "unity-pool-windows-vm" image family.
#
# Usage:
#   ./build-windows.sh --project PROJECT_ID [--credentials-file PATH]
#
# Options:
#   --project           GCP project ID (required, or set GCP_PROJECT_ID env var)
#   --credentials-file  Path to GCP service account JSON (optional, uses ADC if omitted)
#   -h, --help          Show this help
#
# Prerequisites:
#   GCP firewall rule for WinRM (create once):
#     gcloud compute firewall-rules create allow-winrm \
#       --allow tcp:5986 --target-tags allow-winrm --project YOUR_PROJECT
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PACKER_DIR="$REPO_ROOT/communication/infra/scripts/windows-vm-custom-image/packer"

PROJECT_ID="${GCP_PROJECT_ID:-}"
CREDENTIALS_FILE=""

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
        --project)       PROJECT_ID="$2"; shift 2 ;;
        --credentials-file) CREDENTIALS_FILE="$2"; shift 2 ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "Unknown argument: $1" ;;
    esac
done

[[ -n "$PROJECT_ID" ]] || die "--project is required (or set GCP_PROJECT_ID)"
command -v packer &>/dev/null || die "packer not found. Install from https://developer.hashicorp.com/packer/downloads"
[[ -d "$PACKER_DIR" ]] || die "Packer directory not found: $PACKER_DIR"

# =============================================================================
# Build
# =============================================================================

echo "=========================================="
echo "  Building Pool Windows VM Image"
echo "=========================================="
echo ""
echo "  Project:     $PROJECT_ID"
echo "  Packer dir:  $PACKER_DIR"
if [[ -n "$CREDENTIALS_FILE" ]]; then
    echo "  Credentials: $CREDENTIALS_FILE"
else
    echo "  Credentials: Application Default Credentials"
fi
echo ""
echo "  NOTE: Windows builds take ~60 min (Office install)"
echo ""

cd "$PACKER_DIR"

echo "=== Initializing Packer plugins ==="
packer init .
echo ""

echo "=== Running Packer build ==="
PACKER_ARGS=(-var "project_id=$PROJECT_ID")
if [[ -n "$CREDENTIALS_FILE" ]]; then
    PACKER_ARGS+=(-var "credentials_file=$CREDENTIALS_FILE")
fi

packer build "${PACKER_ARGS[@]}" windows-pool-vm.pkr.hcl

echo ""
echo "=========================================="
echo "  Pool Windows image build complete!"
echo "=========================================="
echo ""
echo "The image is now the latest in family: unity-pool-windows-vm"
echo "New pool VMs will automatically use this image."
