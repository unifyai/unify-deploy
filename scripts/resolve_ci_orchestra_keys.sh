#!/usr/bin/env bash
# Populate ORCHESTRA_ADMIN_KEY and UNIFY_KEY for CI when GitHub did not inject
# them. Prefer the in-cluster unity-secrets copy (the runner already has get
# on staging Secrets), then Secret Manager in the runtime project.
#
# Whichever source wins, the key is then probed against Orchestra before the
# suite starts. Presence is not validity: a revoked key resolves fine here and
# then fails minutes later as a bare 401 inside a test fixture, which reads as
# a product failure rather than a credential one. Probing turns that into an
# explicit error naming the key and the source it came from.
set -euo pipefail

project="${TEST_GCP_PROJECT_ID:?TEST_GCP_PROJECT_ID is required}"
orchestra_url="${TEST_ORCHESTRA_URL:?TEST_ORCHESTRA_URL is required}"
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

# HTTP status of an authenticated Orchestra call; 000 when it never answered.
probe() {
  local value="$1"
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    "${orchestra_url}/assistant" \
    -H "Authorization: Bearer ${value}" || true
}

verify() {
  local name="$1"
  local value="$2"
  local source="$3"
  local code=""
  code="$(probe "${value}")"

  case "${code}" in
  401 | 403)
    echo "::error::${name} from ${source} was rejected by Orchestra at" \
      "${orchestra_url} (HTTP ${code}). The key is invalid or revoked. Mint a" \
      "fresh key for this deployment -- copying another stored copy of" \
      "${name} generally reproduces this, because the stored copies go stale" \
      "together."
    exit 1
    ;;
  000 | 5??)
    # A refusal to answer says nothing about the key, so it must not fail the
    # gate here; the suite will report the outage on its own terms.
    echo "${name} from ${source}: unvalidated, Orchestra returned ${code}."
    ;;
  *)
    echo "${name} from ${source}: accepted by Orchestra (HTTP ${code})."
    ;;
  esac
}

resolve() {
  local name="$1"
  local current="${!name:-}"
  if [ -n "${current}" ]; then
    echo "${name} is set (length ${#current})."
    verify "${name}" "${current}" "the workflow environment"
    return 0
  fi

  local value=""
  value="$(from_k8s "${name}" || true)"
  if [ -n "${value}" ]; then
    echo "${name} was empty; read from unity-secrets/${namespace}"
    publish "${name}" "${value}"
    verify "${name}" "${value}" "unity-secrets/${namespace}"
    return 0
  fi

  echo "${name} was empty; reading Secret Manager in ${project}"
  value="$(from_secret_manager "${name}")"
  if [ -z "${value}" ]; then
    echo "::error::Could not resolve ${name} from GitHub, unity-secrets, or Secret Manager."
    exit 1
  fi
  publish "${name}" "${value}"
  verify "${name}" "${value}" "Secret Manager/${project}"
}

resolve ORCHESTRA_ADMIN_KEY
resolve UNIFY_KEY
