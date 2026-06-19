# Setup — Matterport Connection

Paste-and-go: customer generates an API token in Matterport, pastes
both halves into Console.  No OAuth callback.

1. Console -> Integrations -> Add new -> Matterport.
2. Customer opens https://my.matterport.com -> Account -> API Access ->
   **Add API Token**.  Copy **Token ID** and **Token Secret** (Secret
   shown once).
3. Paste both into the modal and Save.

Calls authenticate via HTTP Basic.  **The Token ID and Token Secret are
API tokens, not OAuth client credentials** — send them as HTTP Basic on
every call, never exchange them at `/api/oauth/token` for a Bearer
token.  A `401 invalid_client` from the OAuth endpoint means the wrong
endpoint was hit, not that the tokens are bad.

## Production vs sandbox

- **Sandbox** (free): only sees Matterport's demo models.  Useful for
  end-to-end testing.
- **Production**: requires the **Developer Tools add-on** on the
  customer's plan.  At time of writing, Matterport offers a free annual
  renewal of Developer Tools licenses for a limited window — re-check at
  contract time.

`probe_matterport_tier(force=True, mock=False)` records which surfaces
the active credentials cover; cached in `Matterport/Meta/Capabilities`
for 24h.

## Optional secrets

| Secret | Purpose |
|---|---|
| `MATTERPORT_ORG_ID` | Pin the active org when credentials see more than one |
| `MATTERPORT_BASE_URL` | API host override (default `https://api.matterport.com`) |
| `MATTERPORT_REQUEST_TIMEOUT_SECONDS` / `_RATE_LIMIT_MAX_RETRIES` / `_RATE_LIMIT_BACKOFF_FACTOR` | HTTP behaviour (`30` / `3` / `1.5`) |

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Matterport is not connected for this assistant.` | One of `MATTERPORT_TOKEN_ID` / `MATTERPORT_TOKEN_SECRET` unset | Paste both halves in Console |
| `403` on view-stats or sync | Sandbox token (demo-only) or no Developer Tools add-on | Upgrade Matterport plan |
| `401` unexpectedly | Token ID and Secret swapped, or Secret regenerated | Re-paste both values |
| `401 invalid_client` from `/api/oauth/token` | Tried OAuth token-exchange flow with API tokens | These are HTTP Basic credentials — call the GraphQL endpoint directly, no exchange step |
