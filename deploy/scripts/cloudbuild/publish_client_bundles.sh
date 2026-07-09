#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${1:-staging}"
BUCKET="${UNITY_CLIENT_BUNDLE_BUCKET:-unity-client-bundles}"
SHA="${2:-$(git rev-parse HEAD)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

# unify_company is a symlink into the brain submodule (third_party/brain/unify_deploy).
# Cloud Build does not recurse private submodules by default; init with the
# same DEVBOT token used for other private GitHub clones.
_ensure_brain_submodule() {
  if [ -f third_party/brain/unify_deploy/__init__.py ]; then
    return 0
  fi
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "GITHUB_TOKEN is required to init private submodule third_party/brain" >&2
    exit 1
  fi
  local pin
  pin="$(git ls-tree HEAD third_party/brain | awk '{print $3}')"
  if [ -z "$pin" ]; then
    echo "Could not resolve pinned SHA for third_party/brain" >&2
    exit 1
  fi
  git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
  rm -rf third_party/brain
  mkdir -p third_party
  # Fetch the exact pinned commit (not a shallow tip-of-main clone).
  git clone "https://${GITHUB_TOKEN}@github.com/unifyai/brain.git" third_party/brain
  git -C third_party/brain checkout --detach "$pin"
}

if [ -f .gitmodules ] && grep -q 'third_party/brain' .gitmodules; then
  _ensure_brain_submodule
  if [ ! -f unity_deploy/assistant_deployments/clients/unify_company/__init__.py ]; then
    echo "unify_company symlink does not resolve after submodule init" >&2
    exit 1
  fi
fi

CLIENTS=(
  client_alpha
  clientepsilon_homes
  clientzeta
  client_beta
  unify_company
)

for client in "${CLIENTS[@]}"; do
  src="unity_deploy/assistant_deployments/clients/${client}"
  if [ ! -e "$src" ]; then
    echo "Skipping missing client tree: $src" >&2
    continue
  fi
  archive="/tmp/${client}-${SHA}.tar.gz"
  # -h follows symlinks so the tarball contains real files (H1).
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
