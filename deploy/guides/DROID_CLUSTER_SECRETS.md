# Droid cluster secrets (GCP Secret Manager → GKE)

Droid Job pods read API keys from the Kubernetes Secret `droid-secrets` via `secretKeyRef` (see `communication/infra/helpers.py`). **GCP Secret Manager is the only place to rotate values.** The cluster must mirror `versions/latest`; never `kubectl apply` a hand-built secret YAML with key material.

## Architecture

| Layer | Role |
|-------|------|
| **GCP Secret Manager** (`gcp-project-runtime`) | Source of truth; same secrets Cloud Run Adapters/Comms use |
| **External Secrets Operator** | Polls SM `latest` → `droid-secrets` per namespace (`refreshInterval: 1h`) |
| **`setup_k8s_config.py --update`** | Break-glass reconcile when ESO is not installed or not owning the secret |

`TAVILY_API_KEY` in the cluster uses the same Secret Manager id (`TAVILY_API_KEY`).

## One-time cluster bootstrap (ESO)

Requires `helm`, `gcloud`, and cluster admin on the **droid** GKE cluster. Run once per cluster:

```bash
cd ~/Unify/droid-deploy
chmod +x deploy/scripts/kubernetes/install_external_secrets_operator.sh
./deploy/scripts/kubernetes/install_external_secrets_operator.sh
```

The install script:

- Installs External Secrets Operator (Helm) into `external-secrets`
- Creates `external-secrets/external-secrets-gcp-credentials` from GCP `gcp-sa-key`
- Applies `ClusterSecretStore` `droid-gcp-secret-manager`

**Auth:** ESO reads Secret Manager using the `gcp-sa-key` JSON (in-cluster). If you rotate `gcp-sa-key` in Secret Manager, re-run the install script so `external-secrets-gcp-credentials` is refreshed.

Per-environment `ExternalSecret` resources are applied automatically on Droid deploy via Cloud Build (`apply-droid-external-secrets` in `deploy/cloudbuild-staging.yaml` and `deploy/cloudbuild.yaml`). You can also apply manually:

```bash
kubectl apply -f deploy/k8s/secrets/droid-secrets-external-secret_staging.yaml
kubectl apply -f deploy/k8s/secrets/droid-secrets-external-secret_production.yaml
```

Verify:

```bash
kubectl get externalsecret -n staging
kubectl describe externalsecret droid-secrets -n staging
```

## Rotating an API key

1. Add a new **version** in GCP Secret Manager (do not edit K8s by hand).
2. Wait for ESO (`refreshInterval` 1h) or force an immediate SM → K8s sync:

   ```bash
   kubectl annotate externalsecret droid-secrets -n staging \
     force-sync=$(date +%s) --overwrite
   ```

3. **Restart Droid pods** — env vars are fixed at container start:

   ```bash
   export ADAPTERS_URL=https://service.a.run.app
   export ORCHESTRA_ADMIN_KEY="$(gcloud secrets versions access latest \
     --secret=ORCHESTRA_ADMIN_KEY --project=gcp-project-runtime)"

   curl -sf -X POST -H "Authorization: Bearer $ORCHESTRA_ADMIN_KEY" \
     "$ADAPTERS_URL/scheduled/jobs/create?refresh=true"
   ```

While ESO owns `droid-secrets`, `setup_k8s_config.py --update` refuses to overwrite it (see `reconcile.external-secrets.io/data-hash` on the Secret).

## Break-glass (no ESO)

Before ESO is installed, or if the operator is down:

```bash
python deploy/scripts/kubernetes/setup_k8s_config.py --namespace staging --create
python deploy/scripts/kubernetes/setup_k8s_config.py --namespace staging --update
```

Requires local `gcloud` auth with `secretmanager.versions.access` and GKE admin.

## What not to do

- Do not store API keys in git or paste them into `last-applied-configuration` manifests.
- Do not assume `setup_k8s_config.py --create` updates existing secrets (use `--update` or ESO).
- Do not rotate only GitHub Actions / CI secrets for GKE runtime — those are separate from `droid-secrets`.
