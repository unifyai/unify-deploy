# Shared Cloud Run deploy helpers for the deploy orchestrator.
# Source this file (it defines functions only; it is not executable):
#   source deploy/scripts/cloudbuild/cloud_run_lib.sh
#
# Provides:
#   run_cloud_run_with_retry <cmd...>   -- retry a gcloud run call through the
#                                          transient "ABORTED: Conflict" window
#   repair_traffic_state <service> <region>
#   route_canonical_traffic <service> <region> <build_id>

run_cloud_run_with_retry() {
  local attempt max_attempts delay output output_file sleep_for status
  max_attempts=5
  delay=5
  for attempt in $(seq 1 "$max_attempts"); do
    output_file=$(mktemp)
    set +e
    (set -e; "${@}") > "$output_file" 2>&1
    status=$?
    set -e
    output=$(cat "$output_file")
    rm -f "$output_file"
    printf '%s\n' "$output"
    if [ "$status" -eq 0 ]; then
      return 0
    fi
    if [[ "$output" != *"ABORTED: Conflict for resource"* || "$output" != *"was specified but current version is"* || "$attempt" -eq "$max_attempts" ]]; then
      return "$status"
    fi
    sleep_for=$((delay + RANDOM % 5))
    echo "Cloud Run service changed concurrently; retrying in ${sleep_for}s before attempt $((attempt + 1))/$max_attempts" >&2
    sleep "$sleep_for"
    delay=$((delay * 2))
  done
}

# Preserve the currently-live traffic split/tags before a deploy so a new
# revision does not silently steal 100% traffic from a manually-pinned one.
repair_traffic_state() {
  local service="$1" region="$2"
  local service_json traffic_file
  service_json=$(mktemp)
  traffic_file=$(mktemp)
  gcloud run services describe "$service" \
    --region="$region" \
    --platform=managed \
    --format=json > "$service_json"
  python3 - "$service_json" > "$traffic_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as service_file:
    service = json.load(service_file)

revisions: list[str] = []
status_tags: dict[str, str] = {}
for entry in service.get("status", {}).get("traffic", []):
    revision = entry.get("revisionName")
    if not revision:
        continue
    percent = entry.get("percent")
    if percent is not None and int(percent) > 0:
        revisions.append(f"{revision}={int(percent)}")
    tag = entry.get("tag")
    if tag:
        status_tags[tag] = revision

if not revisions:
    raise SystemExit("No live traffic revision found to preserve")

spec_tags = {
    entry["tag"]: entry.get("revisionName")
    for entry in service.get("spec", {}).get("traffic", [])
    if entry.get("tag")
}
update_tags = [
    f"{tag}={revision}"
    for tag, revision in sorted(status_tags.items())
    if spec_tags.get(tag) != revision
]
remove_tags = sorted(tag for tag in spec_tags if tag not in status_tags)

print("--to-revisions=" + ",".join(revisions))
if update_tags:
    print("--update-tags=" + ",".join(update_tags))
if remove_tags:
    print("--remove-tags=" + ",".join(remove_tags))
PY
  local traffic_args
  mapfile -t traffic_args < "$traffic_file"
  rm -f "$service_json" "$traffic_file"
  run_cloud_run_with_retry gcloud run services update-traffic "$service" \
    --region="$region" \
    --platform=managed \
    "${traffic_args[@]}" \
    --quiet
}

# Route 100% traffic to the revision this build just created (identified by the
# gcb-build-id label), skipping if a newer build already owns the latest revision.
route_canonical_traffic() {
  local service="$1" region="$2" build_id="$3"
  local revision revision_build_id
  revision=$(gcloud run services describe "$service" \
    --region="$region" \
    --platform=managed \
    --format="value(status.latestCreatedRevisionName)")
  if [ -z "$revision" ]; then
    echo "No deployed revision found for build $build_id" >&2
    return 1
  fi
  revision_build_id=$(gcloud run revisions describe "$revision" \
    --region="$region" \
    --format="value(metadata.labels.gcb-build-id)")
  if [ "$revision_build_id" != "$build_id" ]; then
    echo "Skipping traffic route for $revision because it belongs to build $revision_build_id"
    return 0
  fi
  gcloud run services update-traffic "$service" \
    --region="$region" \
    --platform=managed \
    --to-revisions="$revision=100" \
    --quiet
}
