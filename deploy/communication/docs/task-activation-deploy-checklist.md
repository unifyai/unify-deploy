# Task Activation Deployment Checklist

Task activation depends on Orchestra projection, Communication materialization,
Cloud Tasks delivery, and the live/offline callback targets. Run these checks
after deploying Communication, Orchestra, or adapters changes that affect
scheduled tasks.

## Required Cloud Tasks IAM

The Communication runtime service account must be able to inspect queues,
create queues, create tasks, and delete stale tasks. The current runtime uses
the `GCP_SA_KEY` credential loaded by `communication.infra.runtime_clients`.

Grant either a custom role with these permissions or `roles/cloudtasks.admin`:

```bash
gcloud projects add-iam-policy-binding gcp-project-runtime \
  --member="serviceAccount:service-account@example.iam.gserviceaccount.com" \
  --role="roles/cloudtasks.admin"
```

## Queue Names

Production queues:

- `droid-task-due`
- `droid-task-offline`
- `droid-task-activation-repair`

Staging queues:

- `droid-task-due-staging`
- `droid-task-offline-staging`
- `droid-task-activation-repair-staging`

Communication lazily creates missing queues during materialization, so the
runtime identity needs queue create permission before the first scheduled task
is projected.

## Post-Deploy Validation

Validate queue access and callback targets:

```bash
curl -fsS \
  -H "Authorization: Bearer ${ORCHESTRA_ADMIN_KEY}" \
  "https://<comms-host>/infra/task-activation/validate" | jq .
```

Every queue should report `status: "ok"`. A `permission_denied` status means
the runtime cannot use Cloud Tasks and scheduled activation materialization will
fail.

Diagnose one projected task:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer ${ORCHESTRA_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  "https://<comms-host>/infra/task-activation/diagnose" \
  -d '{"assistant_id":"<assistant-id>","task_id":<task-id>}' | jq .
```

For a healthy scheduled activation, `activation` is present and
`materialization.cloud_task_status` is `present`.

## Recovery Order

1. Fix Cloud Tasks IAM and rerun `/infra/task-activation/validate`.
2. Reproject the affected task through Orchestra's admin repair endpoint.
3. Rerun `/infra/task-activation/diagnose` and verify the Cloud Task is present.
4. Manually execute any missed occurrence once through Droid with the original
   scheduled timestamp in the request context.
