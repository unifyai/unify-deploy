#!/usr/bin/env bash
set -euo pipefail
#
# Create (or reuse) the OpenReplay GCE VM and print install steps.
#
# Usage:
#   ./deploy/scripts/dev/provision_openreplay_vm.sh [staging|production]
#
# Official OpenReplay GCP path is a dedicated VM (not Autopilot GKE):
#   https://docs.openreplay.com/en/deployment/deploy-gcp/
#
# After the VM is up:
#   1. Point DNS A record openreplay[-staging].unify.ai at the VM external IP
#   2. SSH in and run openreplay-cli install with DOMAIN_NAME
#   3. Patch vars.yaml s3: block for GCS (see vars.yaml.example)
#   4. Copy project key into Console Secret Manager /
#      NEXT_PUBLIC_OPENREPLAY_PROJECT_KEY
#

ENV="${1:-staging}"
PROJECT_ID="${UNITY_OPENREPLAY_PROJECT_ID:-gcp-project-runtime}"
ZONE="${UNITY_OPENREPLAY_ZONE:-us-central1-a}"

if [[ "${ENV}" == "production" ]]; then
  VM_NAME="unity-openreplay"
  DOMAIN_HINT="openreplay.unify.ai"
else
  VM_NAME="unity-openreplay-staging"
  DOMAIN_HINT="openreplay.example.com"
fi

MACHINE_TYPE="${UNITY_OPENREPLAY_MACHINE_TYPE:-n2-standard-2}"
DISK_SIZE_GB="${UNITY_OPENREPLAY_DISK_GB:-100}"

echo "=== OpenReplay VM (env=${ENV}) ==="
echo "Project: ${PROJECT_ID}"
echo "VM:      ${VM_NAME} (${MACHINE_TYPE}, zone ${ZONE})"
echo "Domain:  ${DOMAIN_HINT}"
echo ""

if gcloud compute instances describe "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" >/dev/null 2>&1; then
  echo "VM already exists."
else
  echo "Creating VM..."
  gcloud compute instances create "${VM_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --boot-disk-size="${DISK_SIZE_GB}GB" \
    --boot-disk-type=pd-balanced \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --tags=openreplay,https-server,http-server \
    --scopes=cloud-platform
fi

EXTERNAL_IP="$(
  gcloud compute instances describe "${VM_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
)"

echo ""
echo "External IP: ${EXTERNAL_IP}"
echo ""
echo "DNS (project gcp-project-dns, zone unifyai):"
echo "  gcloud dns record-sets create ${DOMAIN_HINT}. \\"
echo "    --rrdatas=${EXTERNAL_IP} --type=A --ttl=300 \\"
echo "    --zone=unifyai --project=gcp-project-dns"
echo "  # or update if the record already exists"
echo ""
echo "SSH + install (use IAP — public SSH is not open):"
echo "  gcloud compute ssh ${VM_NAME} --project=${PROJECT_ID} --zone=${ZONE} --tunnel-through-iap"
echo "  sudo wget https://raw.githubusercontent.com/openreplay/openreplay/main/scripts/helmcharts/openreplay-cli -O /bin/openreplay"
echo "  sudo chmod +x /bin/openreplay"
echo "  sudo openreplay -i ${DOMAIN_HINT}"
echo ""
echo "Then configure GCS external storage (vars.yaml s3 block) using"
echo "deploy/k8s/openreplay/vars.yaml.example and reinstall backends:"
echo "  sudo openreplay -e   # edit vars"
echo "  sudo openreplay -R   # reinstall / apply"
echo ""
echo "Firewall: ensure tcp/80 and tcp/443 reach the VM (tag https-server /"
echo "http-server, or an explicit allow rule)."
