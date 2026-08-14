#!/usr/bin/env bash
# Populate ORCHESTRA_ADMIN_KEY and UNIFY_KEY for CI when GitHub did not inject
# them. Prefer the in-cluster unity-secrets copy (the runner already has get
# on staging Secrets), then Secret Manager in the runtime project.
set -euo pipefail

project="${TEST_GCP_PROJECT_ID:?TEST_GCP_PROJECT_ID is required}"
namespace="${TEST_NAMESPACE:-staging}"

publish() {
  local name="$1"
  local value="$2"
  if [ -z "${value}" ]; then
    return 1
  fi
  echo "::add-mask::${value}"
  {
    echo "${name}<<EOF"
    printf '%s\n' "${value}"
    echo "EOF"
  } >> "${GITHUB_ENV}"
}

from_k8s() {
  local name="$1"
  kubectl get secret unity-secrets -n "${namespace}" \
    -o "jsonpath={.data.${name}}" 2>/dev/null | base64 --decode
}

from_secret_manager() {
  local name="$1"
  gcloud secrets versions access latest --secret="${name}" --project="${project}"
}

resolve() {
  local name="$1"
  local current="${!name:-}"
  if [ -n "${current}" ]; then
    echo "${name} is set (length ${#current})."
    return 0
  fi

  local value=""
  value="$(from_k8s "${name}" || true)"
  if [ -n "${value}" ]; then
    echo "${name} was empty; read from unity-secrets/${namespace}"
    publish "${name}" "${value}"
    return 0
  fi

  echo "${name} was empty; reading Secret Manager in ${project}"
  value="$(from_secret_manager "${name}")"
  if [ -z "${value}" ]; then
    echo "::error::Could not resolve ${name} from GitHub, unity-secrets, or Secret Manager."
    exit 1
  fi
  publish "${name}" "${value}"
}

resolve ORCHESTRA_ADMIN_KEY
resolve UNIFY_KEY
