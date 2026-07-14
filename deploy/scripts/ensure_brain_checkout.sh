#!/usr/bin/env bash
# Ensure ``third_party/brain`` is a floating checkout of the brain branch tip.
#
# Not a git submodule and never committed. Used only so the local
# ``clients/unify_company`` symlink resolves for embedded-mode / unit tests.
#
# Branch selection (override with ``BRAIN_REF``):
#   unify-deploy ``main``  → brain ``main``
#   anything else          → brain ``staging``
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${ROOT}/third_party/brain"
REPO_URL="${BRAIN_REPO_URL:-https://github.com/unifyai/brain.git}"

if [ -n "${BRAIN_REF:-}" ]; then
  REF="${BRAIN_REF}"
else
  current="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo staging)"
  case "${current}" in
    main|master) REF=main ;;
    *) REF=staging ;;
  esac
fi

mkdir -p "${ROOT}/third_party"
if [ -d "${DEST}/.git" ]; then
  git -C "${DEST}" fetch --depth 1 origin "${REF}"
  git -C "${DEST}" checkout -B "${REF}" "FETCH_HEAD"
else
  rm -rf "${DEST}"
  git clone --depth 1 --branch "${REF}" "${REPO_URL}" "${DEST}"
fi

if [ ! -f "${DEST}/unify_deploy/__init__.py" ]; then
  echo "brain checkout missing unify_deploy/__init__.py at ${DEST}" >&2
  exit 1
fi

echo "third_party/brain → $(git -C "${DEST}" rev-parse --short HEAD) (${REF})"
