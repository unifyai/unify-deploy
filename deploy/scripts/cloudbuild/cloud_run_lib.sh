# Shared Cloud Run deploy helpers for the deploy orchestrator.
# Source this file (it defines functions only; it is not executable):
#   source deploy/scripts/cloudbuild/cloud_run_lib.sh
#
# Provides:
#   run_cloud_run_with_retry <cmd...>   -- retry a gcloud run call through the
#                                          transient "ABORTED: Conflict" window
#   clear_sticky_revision_pins <service> <region>
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
    # Retry only on Cloud Run optimistic-concurrency conflicts.
    if [ "$attempt" -eq "$max_attempts" ] \
      || { [[ "$output" != *"ABORTED: Conflict for resource"* ]] \
        && [[ "$output" != *"was specified but current version is"* ]]; }; then
      return "$status"
    fi
    sleep_for=$((delay + RANDOM % 5))
    echo "Cloud Run service changed concurrently; retrying in ${sleep_for}s before attempt $((attempt + 1))/$max_attempts" >&2
    sleep "$sleep_for"
    delay=$((delay * 2))
  done
}

# CI deploys overwrite 100% traffic onto the new revision. Clear any sticky
# named-revision pin left by a previous interrupted cutover so `gcloud run
# deploy` can allocate traffic to the revision this build creates.
clear_sticky_revision_pins() {
  local service="$1" region="$2"
  gcloud run services update-traffic "$service" \
    --region="$region" \
    --platform=managed \
    --to-latest \
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
