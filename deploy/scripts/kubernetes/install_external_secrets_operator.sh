#!/usr/bin/env bash
# Install External Secrets Operator and Droid GCP Secret Manager wiring (one-time per cluster).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PROJECT_ID="${GCP_PROJECT_ID:-gcp-project-runtime}"
CLUSTER="${GKE_CLUSTER:-droid}"
REGION="${GKE_REGION:-us-central1}"
ESO_VERSION="${ESO_VERSION:-0.14.2}"

gcloud container clusters get-credentials "${CLUSTER}" --region "${REGION}" --project "${PROJECT_ID}"

helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --version "${ESO_VERSION}" \
  --set installCRDs=true \
  --wait

echo "🔐 Creating ESO credentials secret from GCP Secret Manager (gcp-sa-key)..."
kubectl create secret generic external-secrets-gcp-credentials \
  -n external-secrets \
  --from-literal=key.json="$(gcloud secrets versions access latest --secret=gcp-sa-key --project="${PROJECT_ID}")" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "${REPO_ROOT}/deploy/k8s/secrets/cluster-secret-store.yaml"

cat <<'EOF'

✅ External Secrets Operator is installed and the ClusterSecretStore is valid.

Per environment:
  kubectl apply -f deploy/k8s/secrets/droid-secrets-external-secret_staging.yaml
  kubectl apply -f deploy/k8s/secrets/droid-secrets-external-secret_production.yaml

Verify:
  kubectl get externalsecret -n staging
  kubectl describe externalsecret droid-secrets -n staging

After SM rotation, annotate force-sync or wait for refreshInterval, then restart Droid jobs.
See deploy/guides/DROID_CLUSTER_SECRETS.md

EOF
