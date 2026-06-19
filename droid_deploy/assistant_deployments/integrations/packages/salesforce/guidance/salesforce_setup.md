# Setup — Salesforce Connection

OAuth-based. Customer creates a Connected App in their Salesforce org,
pastes its Consumer Key and Consumer Secret into the Integrations modal,
clicks **Save and Connect** once.

Production-only: the OAuth host is fixed to `login.salesforce.com`.
Sandbox (`test.salesforce.com`) and customer My Domain login URLs are
not supported in v0. Connections from sandbox orgs will fail at the
authorize step.

## End-to-end customer flow

1. Console -> assistant -> **Integrations** -> **Add new** -> **Salesforce**.
2. **Step 1** of the modal shows the canonical redirect URI (Copy
   button). URL is derived from `window.location.origin` so it always
   matches the running deployment.
3. Customer goes to Salesforce **Setup -> App Manager -> New Connected
   App**:
   - **Connected App Name** / **API Name** / **Contact Email**: any.
   - Tick **Enable OAuth Settings**.
   - **Callback URL**: paste the redirect URI from Step 1.
   - **Selected OAuth Scopes**: add at minimum
     - `Manage user data via APIs (api)`
     - `Perform requests at any time (refresh_token, offline_access)`
   - Under the security policy checkboxes, tick **Require Secret for Web Server Flow** and **Require Secret for Refresh Token Flow** only — leave the rest (including any PKCE / proof-key requirement) unticked.
   - Save and wait ~5 minutes for the new app to propagate (Salesforce
     warns about this on the save screen).
   - Open the new app -> **Manage Consumer Details** to reveal the
     Consumer Key + Consumer Secret.
4. Step 2 of the modal: paste both, click **Save and Connect**.
5. Customer redirected to Salesforce for consent, then back to the
   integrations tab as **Connected**.

After Connect, the OAuth callback writes `SALESFORCE_REFRESH_TOKEN`
and `SALESFORCE_INSTANCE_URL`. The `instance_url` is the per-org API
host (e.g. `https://acme.my.salesforce.com`); all REST traffic uses it
as the base. Token exchange / refresh continues to use
`login.salesforce.com`.

## Optional secrets

| Secret | Purpose |
|---|---|
| `SALESFORCE_API_VERSION` | REST API version (default `v60.0`). |
| `SALESFORCE_SYNC_OBJECTS` | Comma-separated object keys to include on sync (default `accounts,contacts,leads,opportunities,cases`). |
| `SALESFORCE_SYNC_OBJECT_INTERVALS` | Per-object cadence overrides (`accounts:3600,opportunities:300`). |
| `SALESFORCE_API_PAGE_SIZE` | SOQL `LIMIT` per page (default 200). |
| `SALESFORCE_REQUEST_TIMEOUT_SECONDS` / `_RATE_LIMIT_MAX_RETRIES` / `_RATE_LIMIT_BACKOFF_FACTOR` | HTTP behaviour (`30` / `3` / `1.5`). |

## Verifying after Connect

1. `get_salesforce_me(mock=False)` — confirms the connection works and
   identifies the org + connected user.
2. `salesforce_request("GET", "sobjects/Account", params={"limit": 5})` —
   quick read against the Account object.  (For SOQL with a `WHERE`
   clause, prefer `run_salesforce_soql("SELECT Id, Name FROM Account
   LIMIT 5")`.)
3. `run_salesforce_sync_tick(full=true, mock=false)` — manual bootstrap.

## Reconnect

Salesforce refresh tokens do not expire by default but can be revoked
by the org admin or invalidated by the Connected App's session policy.
When invalid, the user clicks **Reconnect** in Console -> Integrations.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Salesforce is not connected for this assistant.` | Missing `CLIENT_ID` / `CLIENT_SECRET` / `REFRESH_TOKEN` / `INSTANCE_URL` | Connect or Reconnect. |
| `OAuth refresh failed — reconnect required.` | Refresh token revoked or rotated | Reconnect. |
| Salesforce consent rejects redirect (`redirect_uri_mismatch`) | Callback URL on the Connected App doesn't match what Console sends | Re-open modal, copy Step-1 URI exactly into the Connected App's Callback URL field. |
| `error=invalid_grant` on first Connect | Connected App still propagating after creation (Salesforce warns ~5 min delay) | Wait, retry. |
| `403` on specific objects | Connecting user's profile lacks the object permission, or the Connected App is restricted by IP / login policy | Have the Salesforce admin grant Read on the object, or relax the App's IP/login restrictions. |
| `429` rate-limit / API daily limit hit | Sync too aggressive for the org's API allowance | Increase per-object intervals via `SALESFORCE_SYNC_OBJECT_INTERVALS`, or reduce `SALESFORCE_SYNC_OBJECTS`. |
