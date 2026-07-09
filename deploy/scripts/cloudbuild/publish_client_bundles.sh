#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${1:-staging}"
BUCKET="${UNITY_CLIENT_BUNDLE_BUCKET:-unity-client-bundles}"
SHA="${2:-$(git rev-parse HEAD)}"
CLIENTS=(
  client_alpha
  clientepsilon_homes
  clientzeta
  client_beta
  unify_company
)

for client in "${CLIENTS[@]}"; do
  src="unity_deploy/assistant_deployments/clients/${client}"
  if [ ! -d "$src" ]; then
    echo "Skipping missing client tree: $src" >&2
    continue
  fi
  archive="/tmp/${client}-${SHA}.tar.gz"
  tar -czf "$archive" -C "$src" .
  sha256="$(sha256sum "$archive" | awk '{print $1}')"
  gsutil cp "$archive" "gs://${BUCKET}/${ENVIRONMENT}/${client}/${SHA}.tar.gz"
  printf '%s' "$sha256" | gsutil cp - "gs://${BUCKET}/${ENVIRONMENT}/${client}/${SHA}.sha256"
  printf '%s' "$SHA" | gsutil cp - "gs://${BUCKET}/${ENVIRONMENT}/${client}/latest.txt"
  echo "Published ${client} bundle ${SHA} to gs://${BUCKET}/${ENVIRONMENT}/${client}/"
done
