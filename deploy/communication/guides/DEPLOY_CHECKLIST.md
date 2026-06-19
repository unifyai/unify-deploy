# Deploy Checklist: Lease-Based Assignment Migration

This document tracks the infrastructure changes required when merging
the Lease-based container assignment work into staging and production.

---

## 1. Deprecated: `droid-startup` Pub/Sub topic

The `droid-startup` (and `droid-startup-staging`) topic is **no longer
used** in the startup path. The current design works as follows:

- `/infra/job/start` uses a **K8s Lease + CAS** to atomically claim an
  idle container by patching its Job labels and annotations.
- Containers **poll** `GET /infra/job/{JOB_NAME}` every 500ms to detect
  assignment (no Pub/Sub subscription).
- `droid-pending-startups` is only used when the pool is exhausted
  (overflow queue), not as the primary startup mechanism.

### What still references `droid-startup`

| Location | Status |
|----------|--------|
| `scripts/local.sh` | Creates topic in Pub/Sub emulator — **remove** |
| `guides/INFRA.md`, `guides/infra/*.md` | Describe the old flow — **update** |
| Droid Grafana dashboards | Legacy metric references — **update when convenient** |

### Action items

- [ ] Delete the `droid-startup` and `droid-startup-staging` topics and
      subscriptions from GCP Pub/Sub after confirming no other service
      references them.
- [ ] Update `scripts/local.sh` to stop creating the topic (done in this
      branch).
- [ ] Update guide documentation to reflect the new Lease + CAS + polling
      model.

---

## 2. Cloud Scheduler: `pending-startups` cron

The `POST /scheduled/pending-startups` endpoint exists in both the
adapters and the Comms App, but the **Cloud Scheduler job was not
previously configured**. It has been added to both `cloudbuild/adapters.yaml`
and `cloudbuild/adapters-staging.yaml` in this branch.

The scheduler runs every minute and calls the adapters endpoint, which
forwards to `POST {COMMS_URL}/infra/pending/process` on the Comms App.
This drains the `droid-pending-startups` Pub/Sub queue and assigns
queued startups to idle containers.

### Action items

- [ ] **Staging**: After deploying the adapters-staging service, verify
      the `pending-startups-staging` Cloud Scheduler job was created:
      ```bash
      gcloud scheduler jobs describe pending-startups-staging \
        --location=us-central1
      ```
- [ ] **Production**: After deploying the adapters service, verify
      the `pending-startups` Cloud Scheduler job was created:
      ```bash
      gcloud scheduler jobs describe pending-startups \
        --location=us-central1
      ```

---

## 3. Production RBAC for K8s Leases

The Comms App uses `CoordinationV1Api` (K8s Leases) for atomic container
assignment. This requires RBAC permissions on the `coordination.k8s.io`
API group in the GKE cluster.

**Staging**: RBAC has already been applied.

**Production**: RBAC has **not** been applied yet. Run the following
commands before merging to `main`:

```bash
# Connect to the production GKE cluster
gcloud container clusters get-credentials unity \
  --region us-central1 \
  --project gcp-project-runtime

# Create the Role granting Lease permissions in the production namespace
kubectl create role lease-manager \
  --verb=get,list,create,update,delete \
  --resource=leases.coordination.k8s.io \
  -n production

# Bind the role to the Comms App service account
kubectl create rolebinding comms-lease-binding \
  --role=lease-manager \
  --serviceaccount=production:comm-sa \
  -n production
```

### Verification

After applying, verify the binding exists:

```bash
kubectl get rolebinding comms-lease-binding -n production -o yaml
```

And test that a Lease can be created:

```bash
kubectl auth can-i create leases.coordination.k8s.io \
  --as=system:serviceaccount:production:comm-sa \
  -n production
# Expected: yes
```

### Action items

- [ ] Apply RBAC commands on the production cluster before merging to `main`
- [ ] Verify with `kubectl auth can-i` that the service account has Lease
      permissions

---

## 4. Pub/Sub topic creation: `droid-pending-startups`

The `droid-pending-startups` (and `droid-pending-startups-staging`) topic
and subscription must exist in the `gcp-project-runtime` GCP project.

If they don't exist yet:

```bash
# Staging
gcloud pubsub topics create droid-pending-startups-staging \
  --project=gcp-project-runtime
gcloud pubsub subscriptions create droid-pending-startups-staging-sub \
  --topic=droid-pending-startups-staging \
  --project=gcp-project-runtime

# Production
gcloud pubsub topics create droid-pending-startups \
  --project=gcp-project-runtime
gcloud pubsub subscriptions create droid-pending-startups-sub \
  --topic=droid-pending-startups \
  --project=gcp-project-runtime
```

### Action items

- [ ] Verify `droid-pending-startups-staging` topic and sub exist
- [ ] Verify `droid-pending-startups` topic and sub exist (before prod deploy)
