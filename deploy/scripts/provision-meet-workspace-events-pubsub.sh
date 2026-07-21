#!/usr/bin/env bash
set -euo pipefail
#
# Provision the Google Meet Workspace Events Pub/Sub topology.
#
# Google delivers Meet Workspace Events (e.g.
# google.workspace.meet.transcript.v2.fileGenerated) only through Cloud
# Pub/Sub. This creates the shared topic Google publishes to, grants the Meet
# push service account publisher access, and wires a push subscription that
# delivers each CloudEvent to the Adapters Meet bridge. Adapters re-emits a
# Unify-shaped, HMAC-signed body into Orchestra native_google ingress.
#
# Mirrors the naming/idempotency conventions in
# deploy/scripts/dev/setup_pipeline_infra.sh: production resources carry no
# suffix; every other environment suffixes with "-${ENV}".
#
# Usage:
#   ./deploy/scripts/provision-meet-workspace-events-pubsub.sh [staging|production]
#
# Prerequisites:
#   - gcloud CLI authenticated against the Comms/Adapters project
#     (gcp-project-runtime) with:
#       pubsub.topics.create, pubsub.topics.setIamPolicy,
#       pubsub.subscriptions.create, iam.serviceAccounts.setIamPolicy,
#       run.services.setIamPolicy
#   - The NATIVE_GOOGLE_WEBHOOK_SECRET Secret Manager entries already exist
#     (see deploy/scripts/provision-meet-workspace-events-pubsub.sh notes and
#     orchestra/deploy/provider-trigger-topology.md).

ENV="${1:-staging}"

# The Meet events topic lives with Adapters and the Gmail notifications topic,
# not in the Orchestra project. The Google -> Pub/Sub -> Adapters push path then
# stays entirely inside one project, identical to Gmail. Orchestra only needs
# the fully-qualified topic name (embedded project) for notificationEndpoint.
PROJECT_ID="${UNITY_PUBSUB_PROJECT_ID:-gcp-project-runtime}"

# Meet push identity, per Google Workspace Events docs. Verify against current
# docs if provisioning starts failing with a publisher-access error:
# https://developers.google.com/workspace/events/guides/create-subscription
MEET_PUSH_SA="meet-api-event-push@system.gserviceaccount.com"

if [[ "${ENV}" == "production" ]]; then
  SUFFIX=""
  ADAPTERS_BASE_URL="${UNITY_ADAPTERS_URL:-https://service.a.run.app}"
  ADAPTERS_SERVICE="unity-adapters"
else
  SUFFIX="-${ENV}"
  ADAPTERS_BASE_URL="${UNITY_ADAPTERS_URL:-https://service.a.run.app}"
  ADAPTERS_SERVICE="unity-adapters-staging"
fi

ADAPTERS_REGION="${UNITY_ADAPTERS_REGION:-us-central1}"

TOPIC="meet-workspace-events${SUFFIX}"
TOPIC_RESOURCE="projects/${PROJECT_ID}/topics/${TOPIC}"
SUBSCRIPTION="meet-workspace-events-push${SUFFIX}"

# Reserved Adapters route for the Meet bridge (owned by the Adapters handler
# ticket). The stub currently returns 503 until the bridge lands; the push
# subscription is safe to create now because no Workspace Events subscription
# targets this topic until native provision goes live.
PUSH_ENDPOINT="${ADAPTERS_BASE_URL%/}/meet/workspace-events"

# OIDC identity Pub/Sub uses to authenticate pushes to Adapters. Reuse the
# Comms/Adapters runtime SA so the bridge can verify a known principal.
PUSH_AUTH_SA="${UNITY_MEET_PUSH_AUTH_SA:-comm-sa@${PROJECT_ID}.iam.gserviceaccount.com}"

echo "=== Meet Workspace Events Pub/Sub Setup (env=${ENV}) ==="
echo "Project:            ${PROJECT_ID}"
echo "Topic:              ${TOPIC_RESOURCE}"
echo "Push subscription:  ${SUBSCRIPTION}"
echo "Push endpoint:      ${PUSH_ENDPOINT}"
echo "Push OIDC identity: ${PUSH_AUTH_SA}"
echo ""

# --- Topic ---
echo "Creating topic: ${TOPIC}"
gcloud pubsub topics create "${TOPIC}" --project="${PROJECT_ID}" \
  2>/dev/null || echo "  (topic already exists)"

# --- Publisher IAM for Google's Meet push service account ---
echo "Granting Pub/Sub Publisher to ${MEET_PUSH_SA} on ${TOPIC}"
gcloud pubsub topics add-iam-policy-binding "${TOPIC}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${MEET_PUSH_SA}" \
  --role="roles/pubsub.publisher"

# --- Let Pub/Sub mint OIDC tokens as the push identity ---
# The Pub/Sub service agent must be able to create tokens for PUSH_AUTH_SA so
# the push subscription can attach an OIDC token Adapters can verify.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
echo "Granting tokenCreator on ${PUSH_AUTH_SA} to ${PUBSUB_SERVICE_AGENT}"
gcloud iam service-accounts add-iam-policy-binding "${PUSH_AUTH_SA}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator"

# --- Allow the push identity to invoke Adapters (harmless if public) ---
echo "Granting run.invoker on ${ADAPTERS_SERVICE} to ${PUSH_AUTH_SA}"
gcloud run services add-iam-policy-binding "${ADAPTERS_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${ADAPTERS_REGION}" \
  --member="serviceAccount:${PUSH_AUTH_SA}" \
  --role="roles/run.invoker" \
  2>/dev/null || echo "  (invoker binding skipped or already present)"

# --- Push subscription -> Adapters Meet bridge ---
echo "Creating push subscription: ${SUBSCRIPTION}"
gcloud pubsub subscriptions create "${SUBSCRIPTION}" \
  --project="${PROJECT_ID}" \
  --topic="${TOPIC}" \
  --push-endpoint="${PUSH_ENDPOINT}" \
  --push-auth-service-account="${PUSH_AUTH_SA}" \
  --push-auth-token-audience="${ADAPTERS_BASE_URL%/}" \
  --ack-deadline=60 \
  --message-retention-duration=7d \
  --expiration-period=never \
  2>/dev/null || {
    echo "  (subscription exists; updating push config)"
    gcloud pubsub subscriptions update "${SUBSCRIPTION}" \
      --project="${PROJECT_ID}" \
      --push-endpoint="${PUSH_ENDPOINT}" \
      --push-auth-service-account="${PUSH_AUTH_SA}" \
      --push-auth-token-audience="${ADAPTERS_BASE_URL%/}"
  }

echo ""
echo "=== Done ==="
echo "Set this on Orchestra (${ENV}) as the native Google Meet topic:"
echo "  NATIVE_GOOGLE_MEET_EVENTS_PUBSUB_TOPIC=${TOPIC_RESOURCE}"
echo "NATIVE_GOOGLE_WEBHOOK_SECRET must hold the SAME value in Secret Manager in"
echo "both projects: gcp-project-saas (Orchestra ingress verify) and ${PROJECT_ID}"
echo "(Adapters bridge signing)."
