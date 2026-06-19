#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_deployment_reconcile_job.sh \
    --environment staging|production \
    --namespace staging|production \
    --image IMAGE \
    --orchestra-url URL \
    --droid-comms-url URL \
    [--planes control-plane] \
    [--concurrency N] \
    [--timeout 600s] \
    [--template deploy/k8s/deployment-reconcile/deployment-reconcile-job.yaml] \
    [--client CLIENT] \
    [--assistant-id ASSISTANT_ID]

Creates a one-off Kubernetes Job that reconciles deploy-time Droid control-plane state,
waits for it, prints logs, and deletes it.
USAGE
}

environment=""
namespace=""
image=""
orchestra_url=""
droid_comms_url=""
planes="control-plane"
concurrency="8"
timeout="600s"
template="deploy/k8s/deployment-reconcile/deployment-reconcile-job.yaml"
client=""
assistant_id=""
brain_operator_assistant_id=""
brain_operator_tasks_enabled=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment="${2:-}"; shift 2 ;;
    --namespace) namespace="${2:-}"; shift 2 ;;
    --image) image="${2:-}"; shift 2 ;;
    --orchestra-url) orchestra_url="${2:-}"; shift 2 ;;
    --droid-comms-url) droid_comms_url="${2:-}"; shift 2 ;;
    --planes) planes="${2:-}"; shift 2 ;;
    --concurrency) concurrency="${2:-}"; shift 2 ;;
    --timeout) timeout="${2:-}"; shift 2 ;;
    --template) template="${2:-}"; shift 2 ;;
    --client) client="${2:-}"; shift 2 ;;
    --assistant-id) assistant_id="${2:-}"; shift 2 ;;
    --brain-operator-assistant-id) brain_operator_assistant_id="${2:-}"; shift 2 ;;
    --brain-operator-tasks-enabled) brain_operator_tasks_enabled="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "Missing required argument: ${name}" >&2
    usage >&2
    exit 2
  fi
}

require_value "--environment" "$environment"
require_value "--namespace" "$namespace"
require_value "--image" "$image"
require_value "--orchestra-url" "$orchestra_url"
require_value "--droid-comms-url" "$droid_comms_url"
require_value "--planes" "$planes"
require_value "--concurrency" "$concurrency"

if [[ ! -f "$template" ]]; then
  echo "Job template not found: ${template}" >&2
  exit 2
fi

case "${environment}:${namespace}" in
  staging:staging)
    if [[ "$orchestra_url" != *staging* ]]; then
      echo "Staging reconciliation requires a staging ORCHESTRA_URL." >&2
      exit 2
    fi
    ;;
  production:production)
    if [[ "$orchestra_url" == *staging* || "$orchestra_url" == *localhost* || "$orchestra_url" == *127.0.0.1* ]]; then
      echo "Production reconciliation requires a production ORCHESTRA_URL." >&2
      exit 2
    fi
    ;;
  *)
    echo "Unsupported environment/namespace pair: ${environment}/${namespace}" >&2
    exit 2
    ;;
esac

parse_timeout_seconds() {
  local value="$1"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$value"
  elif [[ "$value" =~ ^([0-9]+)s$ ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$value" =~ ^([0-9]+)m$ ]]; then
    echo "$((BASH_REMATCH[1] * 60))"
  else
    echo "Unsupported timeout '${value}'. Use seconds, Ns, or Nm." >&2
    exit 2
  fi
}

timeout_seconds="$(parse_timeout_seconds "$timeout")"

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

render_manifest() {
  sed \
    -e "s/__NAMESPACE__/$(escape_sed_replacement "$namespace")/g" \
    -e "s/__ENVIRONMENT__/$(escape_sed_replacement "$environment")/g" \
    -e "s/__IMAGE__/$(escape_sed_replacement "$image")/g" \
    -e "s/__ORCHESTRA_URL__/$(escape_sed_replacement "$orchestra_url")/g" \
    -e "s/__DROID_COMMS_URL__/$(escape_sed_replacement "$droid_comms_url")/g" \
    -e "s/__PLANES__/$(escape_sed_replacement "$planes")/g" \
    -e "s/__CONCURRENCY__/$(escape_sed_replacement "$concurrency")/g" \
    -e "s/__CLIENT__/$(escape_sed_replacement "$client")/g" \
    -e "s/__ASSISTANT_ID__/$(escape_sed_replacement "$assistant_id")/g" \
    -e "s/__BRAIN_OPERATOR_ASSISTANT_ID__/$(escape_sed_replacement "$brain_operator_assistant_id")/g" \
    -e "s/__BRAIN_OPERATOR_TASKS_ENABLED__/$(escape_sed_replacement "$brain_operator_tasks_enabled")/g" \
    "$template"
}

print_job_logs() {
  local job_ref="$1"
  echo "Deployment reconcile logs (${job_ref}):"
  kubectl logs "$job_ref" -n "$namespace" --all-containers=true || true
}

delete_job() {
  local job_ref="$1"
  echo "Deleting deployment reconcile job ${job_ref}..."
  kubectl delete "$job_ref" -n "$namespace" --ignore-not-found=true --wait=false >/dev/null || true
}

echo "Creating deployment reconcile job in namespace ${namespace}..."
job_ref="$(render_manifest | kubectl create -f - -o name)"
echo "Created ${job_ref} using image ${image}."

deadline=$((SECONDS + timeout_seconds))
while true; do
  complete_condition="$(
    kubectl get "$job_ref" -n "$namespace" \
      -o 'jsonpath={range .status.conditions[?(@.type=="Complete")]}{.status}{end}' \
      2>/dev/null || true
  )"
  failed_condition="$(
    kubectl get "$job_ref" -n "$namespace" \
      -o 'jsonpath={range .status.conditions[?(@.type=="Failed")]}{.status}{end}' \
      2>/dev/null || true
  )"

  if [[ "$complete_condition" == "True" ]]; then
    echo "Deployment reconciliation completed."
    print_job_logs "$job_ref"
    delete_job "$job_ref"
    exit 0
  fi

  if [[ "$failed_condition" == "True" ]]; then
    echo "Deployment reconciliation failed." >&2
    print_job_logs "$job_ref"
    kubectl describe "$job_ref" -n "$namespace" || true
    delete_job "$job_ref"
    exit 1
  fi

  if (( SECONDS >= deadline )); then
    echo "Timed out waiting ${timeout} for deployment reconciliation." >&2
    print_job_logs "$job_ref"
    kubectl describe "$job_ref" -n "$namespace" || true
    delete_job "$job_ref"
    exit 1
  fi

  sleep 5
done
