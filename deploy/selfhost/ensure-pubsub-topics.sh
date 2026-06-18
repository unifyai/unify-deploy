#!/usr/bin/env bash
# Ensure Pub/Sub emulator topics and subscriptions for one assistant.
set -euo pipefail

agent_id="${1:-}"
project_id="${PUBSUB_GCP_PROJECT_ID:-local-test-project}"
suffix="${PUBSUB_TOPIC_SUFFIX:--staging}"
emulator_host="${PUBSUB_EMULATOR_HOST:-pubsub-emulator:8085}"

if [[ -z "$agent_id" ]]; then
  exit 0
fi

emulator_url="http://${emulator_host#http://}"
emulator_url="${emulator_url%/}"

topic_name="unity-${agent_id}${suffix}"

topic_http="$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
  "${emulator_url}/v1/projects/${project_id}/topics/${topic_name}")"
if [[ "$topic_http" != "200" && "$topic_http" != "409" ]]; then
  echo "Failed to create topic ${topic_name} (HTTP ${topic_http})" >&2
  exit 1
fi

outbound_sub="${topic_name}-outbound-sub"
curl -sf -o /dev/null \
  -X PUT "${emulator_url}/v1/projects/${project_id}/subscriptions/${outbound_sub}" \
  -H "Content-Type: application/json" \
  -d "{\"topic\":\"projects/${project_id}/topics/${topic_name}\",\"filter\":\"attributes.thread = \\\"unify_message_outbound\\\"\"}" \
  || true

syserr_sub="${topic_name}-system-error-sub"
curl -sf -o /dev/null \
  -X PUT "${emulator_url}/v1/projects/${project_id}/subscriptions/${syserr_sub}" \
  -H "Content-Type: application/json" \
  -d "{\"topic\":\"projects/${project_id}/topics/${topic_name}\",\"filter\":\"attributes.thread = \\\"system_error\\\"\"}" \
  || true

actions_sub="${topic_name}-actions-sub"
curl -sf -o /dev/null \
  -X PUT "${emulator_url}/v1/projects/${project_id}/subscriptions/${actions_sub}" \
  -H "Content-Type: application/json" \
  -d "{\"topic\":\"projects/${project_id}/topics/${topic_name}\",\"filter\":\"attributes.thread = \\\"action_event\\\"\",\"messageRetentionDuration\":\"1800s\"}" \
  || true

inbound_sub="${topic_name}-sub"
existing_filter=""
existing_filter="$(curl -sf \
  "${emulator_url}/v1/projects/${project_id}/subscriptions/${inbound_sub}" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('filter',''))" 2>/dev/null || true)"

if [[ "$existing_filter" == 'attributes.thread = "inbound"' ]]; then
  exit 0
fi

curl -sf -o /dev/null \
  -X DELETE "${emulator_url}/v1/projects/${project_id}/subscriptions/${inbound_sub}" \
  2>/dev/null || true

inbound_http="$(curl -s -o /dev/null -w "%{http_code}" \
  -X PUT "${emulator_url}/v1/projects/${project_id}/subscriptions/${inbound_sub}" \
  -H "Content-Type: application/json" \
  -d "{\"topic\":\"projects/${project_id}/topics/${topic_name}\",\"filter\":\"attributes.thread = \\\"inbound\\\"\"}")"
if [[ "$inbound_http" != "200" && "$inbound_http" != "409" ]]; then
  echo "Failed to create inbound subscription ${inbound_sub} (HTTP ${inbound_http})" >&2
  exit 1
fi
