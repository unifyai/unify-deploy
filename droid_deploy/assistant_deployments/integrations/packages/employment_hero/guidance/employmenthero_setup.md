# Setup — Employment Hero Connection

OAuth-based.  Customer creates an Employment Hero developer-portal app,
pastes its `client_id` and `client_secret` into the Integrations modal,
clicks **Save and Connect**.  Refresh tokens are minted automatically
from then on.

## End-to-end customer flow

1. Console -> assistant -> **Integrations** -> **Add new** -> **Employment
   Hero**.  The Connect modal opens.
2. **Step 1** shows the canonical redirect URI to register in EH (with a
   Copy button).  The URL is derived from `window.location.origin`, so
   it always matches the running deployment.
3. Customer opens https://developer.employmenthero.com, creates/edits
   their app, pastes the redirect URI into **Redirect URIs**, saves, and
   notes the **Client ID** and **Client Secret**.
4. Back in Step 2 of the modal, paste both credentials and click **Save
   and Connect**.
5. Customer is redirected to EH for consent, then back to the
   integrations tab as **Connected**.

After Connect, the OAuth callback writes:

| Secret | Purpose |
|---|---|
| `EMPLOYMENTHERO_REFRESH_TOKEN` | Long-lived; runtime mints access tokens from this |
| `EMPLOYMENTHERO_ORGANISATION_ID` | Active org.  Auto-pinned when exactly one named org is accessible.  When multiple, the success toast points the operator at Settings -> Secrets to override. |

## Optional secrets

| Secret | Purpose |
|---|---|
| `EMPLOYMENTHERO_BASE_URL` | API host override.  Default `https://api.employmenthero.com`; some UK accounts need `https://api.employmenthero.co.uk`. |
| `EMPLOYMENTHERO_OAUTH_TOKEN_URL` | OAuth token-endpoint override |
| `EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS`, `EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES`, `EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR` | HTTP behaviour |

## Verifying after Connect

1. `get_account_info(mock=False)` — confirms the connection works.
2. `probe_tier(force=True, mock=False)` — sweeps endpoints to record
   which capabilities the app's scopes cover.
3. (Optional) Operator flips `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`.
4. `run_employmenthero_sync_tick(full=true, mock=false)` — manual
   bootstrap of DataManager.

If `probe_tier` shows critical-tier capabilities (`pay`) as 403, that's
expected for non-admin EH apps.

## Reconnect

If the user sees a `reconnect required` envelope (refresh token expired,
revoked, or rotated), they click **Reconnect** in Console ->
Integrations.  Same OAuth flow; new refresh token replaces the old.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Employment Hero is not connected for this assistant.` | Missing `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `REFRESH_TOKEN` | Listed `missing_secrets` shows what's needed.  Connect or Reconnect. |
| `refresh failed — reconnect required` | Refresh token invalid | Reconnect. |
| EH consent screen rejects redirect (`invalid_redirect_uri`, `redirect_uri_mismatch`) | Redirect URI in EH app doesn't match what Console sends | Re-open the modal, copy the Step-1 URI exactly into the EH app. |
| `403` on specific capabilities | App lacks scope, or user's EH role doesn't permit it | Edit the app's scope catalogue, Reconnect.  See `employmenthero_tier_gating.md`. |
| `Multiple organisations may be available` | Token has access to >1 org and auto-pin couldn't choose | Run `list_organisations(mock=False)`, set `EMPLOYMENTHERO_ORGANISATION_ID` manually. |

## Region notes

EH operates in UK / AU / NZ / SG / MY.  Default
`api.employmenthero.com` routes regionally for most accounts.  UK
customers occasionally need `EMPLOYMENTHERO_BASE_URL=https://api.employmenthero.co.uk`
on region-mismatch errors (404/401 that doesn't reflect token validity).
