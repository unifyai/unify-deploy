#!/usr/bin/env bash
# Publish *enterprise* client deployment trees to GCS.
#
# ``unify_company`` is owned by the brain repo: pushes to brain/staging and
# brain/main publish that bundle directly. This script must not upload it.
set -euo pipefail

ENVIRONMENT="${1:-staging}"
BUCKET="${UNIFY_CLIENT_BUNDLE_BUCKET:-unity-client-bundles}"
SHA="${2:-$(git rev-parse HEAD)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

CLIENTS=(
  client_alpha
  clientepsilon_homes
  clientzeta
  client_beta
)

for client in "${CLIENTS[@]}"; do
  src="unify_deploy/assistant_deployments/clients/${client}"
  if [ ! -e "$src" ]; then
    echo "Skipping missing client tree: $src" >&2
    continue
  fi
  archive="/tmp/${client}-${SHA}.tar.gz"
  # -h follows symlinks so the tarball contains real files.
  tar -h \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    -czf "$archive" -C "$src" .
  sha256="$(sha256sum "$archive" | awk '{print $1}')"
  gsutil cp "$archive" "gs://${BUCKET}/${ENVIRONMENT}/${client}/${SHA}.tar.gz"
  printf '%s' "$sha256" | gsutil cp - "gs://${BUCKET}/${ENVIRONMENT}/${client}/${SHA}.sha256"
  printf '%s' "$SHA" | gsutil cp - "gs://${BUCKET}/${ENVIRONMENT}/${client}/latest.txt"
  echo "Published ${client} bundle ${SHA} to gs://${BUCKET}/${ENVIRONMENT}/${client}/"
done
