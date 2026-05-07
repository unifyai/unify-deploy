# Setup — Webex Connection

OAuth-based.  Customer creates a Webex Integration app at
https://developer.webex.com, pastes its Client ID and Client Secret into
the Integrations modal, clicks **Save and Connect** once.

## End-to-end customer flow

1. Console -> assistant -> **Integrations** -> **Add new** -> **Webex**.
2. **Step 1** of the modal shows the canonical redirect URI (Copy
   button).  URL is derived from `window.location.origin` so it always
   matches the running deployment.
3. Customer goes to https://developer.webex.com -> **My Webex Apps ->
   Create New App -> Integration**:
   - paste the redirect URI into **Redirect URI(s)**;
   - tick the scopes the assistant should hold:
     - `spark:all` — covers messaging, rooms, people, memberships,
       attachments, teams, devices
     - `meeting:schedules_read`, `meeting:schedules_write`
     - `meeting:participants_read`, `meeting:participants_write`
     - `meeting:recordings_read`, `meeting:recordings_write`
     - `meeting:transcripts_read`
     - `meeting:controls_read`, `meeting:controls_write`
     - `meeting:preferences_read`, `meeting:preferences_write`
   - save and note the Client ID + Client Secret.

   The set above must be a *superset* of what the Console requests on
   the authorize URL — narrowing the app's declaration causes
   `invalid_scope` at consent time.  Admin-level scopes
   (`spark-admin:*`, `meeting:admin_*`) are deliberately not in the
   default; add them only if the customer needs org-wide reads and is
   connecting as an admin.
4. Step 2 of the modal: paste both, click **Save and Connect**.
5. Customer redirected to Webex for consent, then back to the
   integrations tab as **Connected**.

After Connect, the OAuth callback writes `WEBEX_REFRESH_TOKEN`.

## Optional secrets

| Secret | Purpose |
|---|---|
| `WEBEX_BASE_URL` | API host override (default `https://webexapis.com`) |
| `WEBEX_OAUTH_TOKEN_URL` | OAuth token-endpoint override |
| `WEBEX_MIRROR_TRANSCRIPTS` | Set `true` to mirror transcript body text into DataManager.  Off by default — bodies can be sensitive and high-volume. |
| `WEBEX_REQUEST_TIMEOUT_SECONDS` / `_RATE_LIMIT_MAX_RETRIES` / `_RATE_LIMIT_BACKOFF_FACTOR` | HTTP behaviour (`30` / `3` / `1.5`) |
| `WEBEX_MEETING_LOOKBACK_DAYS` | Initial bootstrap lookback (default `90`) |

## Verifying after Connect

1. `get_webex_me(mock=False)` — confirms the connection works.
2. `probe_webex_tier(force=True, mock=False)` — sweeps each Webex
   resource to record which scopes the app covers.  Cached in
   `Webex/Meta/Capabilities` for 24h.
3. `run_webex_sync_tick(full=true, mock=false)` — manual bootstrap.

If `probe_webex_tier` shows `transcripts` as 403, add the
`meeting:transcripts_read` scope to the Webex Integration app and
Reconnect.

## Reconnect

Refresh tokens last 90 days of disuse.  When invalid (expired/revoked/
rotated), the user clicks **Reconnect** in Console -> Integrations.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Webex is not connected for this assistant.` | Missing `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `REFRESH_TOKEN` | Connect or Reconnect |
| `OAuth refresh failed — reconnect required.` | Refresh token invalid | Reconnect |
| Webex consent rejects redirect (`invalid_redirect_uri`) | Redirect URI in Webex app doesn't exactly match what Console sends | Re-open modal, copy Step-1 URI exactly into the Webex app |
| `403` on specific capabilities | App lacks scope, or connecting user's role doesn't permit | Edit scope catalogue at developer.webex.com, Reconnect.  Some org-wide reads require an admin user. |
| `429` rate-limit | Sync too aggressive | Increase `WEBEX_RATE_LIMIT_BACKOFF_FACTOR` or reduce per-object cadence |
