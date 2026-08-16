#!/usr/bin/env bash
# Clone first-party path dependencies next to this repo so uv path sources
# resolve. Rewriting those sources to git URLs is not enough: unify and
# unillm still declare ``unisdk = { path = "../unisdk" }``, and uv then
# asks git for ``#subdirectory=../unisdk``, which is not a valid git path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(cd "${ROOT}/.." && pwd)"
BRANCH="${UNIFY_DEPLOY_CI_FIRST_PARTY_BRANCH:-staging}"

for repo in unify unisdk unillm; do
  dest="${PARENT}/${repo}"
  url="https://github.com/unifyai/${repo}.git"
  if [ -d "${dest}/.git" ]; then
    git -C "${dest}" fetch --depth 1 origin "${BRANCH}"
    git -C "${dest}" checkout -B "${BRANCH}" FETCH_HEAD
  else
    rm -rf "${dest}"
    git clone --depth 1 --branch "${BRANCH}" "${url}" "${dest}"
  fi
  echo "${repo} → $(git -C "${dest}" rev-parse --short HEAD) (${BRANCH})"
done
