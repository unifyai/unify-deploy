#!/usr/bin/env bash
# Drop duplicate-webhook and superseded Cloud Build runs of the same trigger.
#
# Cloud Build has no native concurrency control: GitHub occasionally delivers a
# push webhook twice (producing two identical builds seconds apart), and a rapid
# re-push starts a fresh build while the previous one is still running. Both burn
# builder VMs and can race each other's deploys. This guard runs as the first
# step of every push-triggered build and enforces "newest build for a trigger
# wins":
#   - If a strictly-newer ongoing build exists for the same trigger, this build
#     cancels itself (the duplicate/superseded case).
#   - Otherwise it cancels every strictly-older ongoing build for the trigger and
#     proceeds as the authoritative run.
#
# Fail-open by design: any error inspecting or cancelling builds must never block
# a legitimate deploy, so every failure path exits 0 and lets the build proceed.
#
# Usage: concurrency_guard.sh <region>
# Requires env: BUILD_ID, PROJECT_ID. The build service account needs
# cloudbuild.builds.list and cloudbuild.builds.update (cancel).

set -uo pipefail

REGION="${1:?region required}"
SELF_ID="${BUILD_ID:-}"
PROJECT="${PROJECT_ID:-}"

if [ -z "$SELF_ID" ] || [ -z "$PROJECT" ]; then
  echo "guard: BUILD_ID/PROJECT_ID unset; proceeding"
  exit 0
fi

self_json="$(gcloud builds describe "$SELF_ID" --region="$REGION" --project="$PROJECT" --format=json 2>/dev/null)" || {
  echo "guard: cannot describe self build; proceeding"
  exit 0
}

read_field() { printf '%s' "$self_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }
trigger_id="$(read_field 'd.get("buildTriggerId","")')"
self_create="$(read_field 'd.get("createTime","")')"

if [ -z "$trigger_id" ]; then
  echo "guard: no trigger id (manual/submit run); proceeding"
  exit 0
fi
if [ -z "$self_create" ]; then
  echo "guard: cannot read self createTime; proceeding"
  exit 0
fi

ongoing="$(gcloud builds list --region="$REGION" --project="$PROJECT" --ongoing \
  --filter="buildTriggerId=$trigger_id" --format='value(id,createTime)' 2>/dev/null)" || {
  echo "guard: cannot list ongoing builds; proceeding"
  exit 0
}

newer_exists=false
older_ids=()
while IFS=$'\t' read -r bid bcreate; do
  [ -z "$bid" ] && continue
  [ "$bid" = "$SELF_ID" ] && continue
  # RFC3339 UTC timestamps sort correctly as plain strings. Break exact-tie
  # (duplicate webhooks share a createTime to microseconds only rarely) by id.
  if [[ "$bcreate" > "$self_create" ]]; then
    newer_exists=true
  elif [[ "$bcreate" < "$self_create" ]]; then
    older_ids+=("$bid")
  elif [[ "$bid" > "$SELF_ID" ]]; then
    newer_exists=true
  else
    older_ids+=("$bid")
  fi
done <<< "$ongoing"

if [ "$newer_exists" = true ]; then
  echo "guard: a newer build for trigger $trigger_id is running; cancelling self ($SELF_ID)"
  gcloud builds cancel "$SELF_ID" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1 || true
  # The cancel terminates the build; exit non-zero so nothing else runs if the
  # cancellation has not landed yet.
  sleep 5
  exit 1
fi

for bid in "${older_ids[@]:-}"; do
  [ -z "$bid" ] && continue
  echo "guard: cancelling superseded older build $bid"
  gcloud builds cancel "$bid" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1 || true
done

echo "guard: proceeding as newest build for trigger $trigger_id"
exit 0
