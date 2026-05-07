# Setup — Matterport Connection

Paste-and-go: the customer generates an API token in their Matterport
account, then pastes both halves into Console.  No OAuth callback.

## End-to-end customer flow

1. In Console → assistant → **Integrations**, click **Add new** and
   choose **Matterport**.  The modal opens with two fields.
2. The customer opens https://my.matterport.com in a new tab and goes
   to **Account → API Access**.  They click **Add API Token**, name
   it (e.g. "unity"), and copy the **Token ID** and **Token Secret**
   from the dialog.  The Secret is shown once — save it now.
3. Back in the modal, the customer pastes Token ID into the first
   field and Token Secret into the second field, then clicks **Save**.

The package authenticates each GraphQL call via HTTP Basic using both
values.

## Production vs sandbox

* **Sandbox** is free.  Sandbox tokens only see Matterport's demo
  models — they cannot read the customer's real tours.  Useful for
  package development and end-to-end testing of the assistant with
  mock-shaped data.
* **Production** access requires the **Developer Tools add-on** on the
  customer's Matterport plan.  At time of writing, Matterport offers a
  free annual renewal of Developer Tools licenses for a limited window;
  worth re-checking at contract time.

`probe_matterport_tier(force=True, mock=False)` records which surfaces
the active credentials cover.  Cached in
`Matterport/Meta/Capabilities` for 24 h.

## Optional secrets

| Secret | Purpose |
|---|---|
| `MATTERPORT_ORG_ID` | Pin the active organisation when credentials see more than one. |
| `MATTERPORT_BASE_URL` | API host override (defaults to `https://api.matterport.com`). |
| `MATTERPORT_REQUEST_TIMEOUT_SECONDS` | Per-request HTTP timeout (default 30). |
| `MATTERPORT_RATE_LIMIT_MAX_RETRIES` | 429 retry attempts (default 3). |
| `MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR` | Exponential backoff base (default 1.5). |

## Verifying after Connect

1. `get_matterport_account_info(mock=False)` — confirms the credentials
   work and echoes the active organisation.
2. `probe_matterport_tier(force=True, mock=False)` — sweeps each
   surface to record which capabilities the credentials cover.
3. (Optional) `run_matterport_sync_tick(full=true, mock=false)` for a
   manual bootstrap of DataManager contexts.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Matterport is not connected for this assistant.` | One of `MATTERPORT_TOKEN_ID` / `MATTERPORT_TOKEN_SECRET` is unset | Open Integrations modal; the listed `missing_secrets` show what's needed.  Paste both halves and Save. |
| `403` on view-stats or sync | Sandbox token (only demo models) or no Developer Tools add-on | Customer needs to upgrade Matterport plan; sandbox keeps working for demo data. |
| `429` rate-limit | Sync too aggressive | Increase `MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR` or reduce per-object cadence in `MATTERPORT_SYNC_OBJECT_INTERVALS`. |
| `401` unexpectedly | Token ID and Secret swapped, or Secret was regenerated | Re-paste both values in Console. |
