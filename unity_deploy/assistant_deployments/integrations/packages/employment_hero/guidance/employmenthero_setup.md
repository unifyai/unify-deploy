# Setup — Employment Hero Connection

The Connect flow is OAuth-based.  Customers create their own Employment
Hero developer-portal app, paste its `client_id` and `client_secret`
into the per-assistant Integrations modal, then click **Save and
Connect** once.  unity-deploy refreshes access tokens behind the scenes
from then on.

## End-to-end customer flow

1. In Console → assistant → **Integrations**, click **Add new** and
   choose **Employment Hero**.  The Connect modal opens.
2. **Step 1 in the modal** shows the exact redirect URI to register in
   Employment Hero, with a **Copy** button.  The URL is computed from
   the console origin the customer is on (e.g.
   `https://console.acme.com/oauth/employmenthero/callback`), so it
   always matches the running deployment — no operator hand-off needed.
3. The customer opens https://developer.employmenthero.com in a new
   tab, creates (or edits) their app, pastes the URL into the app's
   **Redirect URIs** field, and saves the app.  They note the **Client
   ID** and **Client Secret**.
4. Back in the modal, **Step 2** asks for the Client ID and Client
   Secret — they paste both and click **Save and Connect**.
5. They're redirected to Employment Hero for consent, then back to the
   integrations tab with the card showing **Connected**.

unity-deploy refreshes access tokens automatically from this point.
The customer clicks **Reconnect** on the same card if/when the refresh
token expires (~60 days for most EH accounts).

### Notes for operators

* The redirect URI is **never** baked into a deployment env var.  It's
  always derived client-side from `window.location.origin`, so the
  customer copies whatever URL the modal shows them.
* The Client ID and Client Secret are stored as per-assistant secrets
  (`EMPLOYMENTHERO_OAUTH_CLIENT_ID`, `EMPLOYMENTHERO_OAUTH_CLIENT_SECRET`)
  — they're per-customer and never shared with other Unify deployments.

After Connect, the OAuth callback writes these secrets automatically:

| Secret | Set by | Purpose |
|---|---|---|
| `EMPLOYMENTHERO_REFRESH_TOKEN` | Console callback | Long-lived token used by runtime to mint access tokens |
| `EMPLOYMENTHERO_ORGANISATION_ID` | Console callback | Active EH organisation, captured from `/me` |
| `EMPLOYMENTHERO_HUB_DOMAIN` | Console callback | Subdomain (e.g. `acme.employmenthero.com`) for UI deep-links |

## Optional secrets

| Secret | Purpose |
|---|---|
| `EMPLOYMENTHERO_BASE_URL` | API host override.  Defaults to `https://api.employmenthero.com`; UK customers occasionally need `https://api.employmenthero.co.uk`. |
| `EMPLOYMENTHERO_OAUTH_TOKEN_URL` | OAuth token-endpoint override.  Defaults to `https://oauth.employmenthero.com/oauth2/token`. |
| `EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS` | Per-request HTTP timeout (default 30). |
| `EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES` | 429 retry attempts (default 3). |
| `EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR` | Exponential backoff base (default 1.5). |

## How runtime auth works

`functions/_client.py` resolves a usable access token at every call:

1. **OAuth refresh path** — if `EMPLOYMENTHERO_OAUTH_CLIENT_ID`,
   `EMPLOYMENTHERO_OAUTH_CLIENT_SECRET`, and `EMPLOYMENTHERO_REFRESH_TOKEN`
   are all set, mint a fresh access token via `oauth2/token` and cache
   it in-process for ~55 minutes (well under EH's typical 60-minute
   access-token lifetime).
2. **Not connected** — any of those three secrets is missing; functions
   return a structured error envelope listing the missing secret names
   so the assistant can tell the user exactly what to do.

The cache lives in worker process memory.  Workers refresh independently
after restart.  No persistent access-token storage — only the
refresh_token is durable.

## Verifying after Connect

Recommended assistant flow on first use:

1. `get_account_info(mock=False)` — confirms the connection works and
   echoes the active organisation.
2. `probe_tier(force=True, mock=False)` — sweeps each EH endpoint to
   record which capabilities the customer's app scopes cover.  Cached
   in `EmploymentHero/Workforce/Meta/Capabilities` for 24 h.
3. (Optional) Operator flips `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`
   if writes should be permitted.
4. `run_employmenthero_sync_tick(full=true, mock=false)` for a manual
   bootstrap of DataManager contexts.

If `probe_tier` shows critical-tier capabilities (`pay`) as 403, that's
expected for non-admin EH apps — those capabilities still work in
mock mode for design/dev work, and live calls return graceful 403
envelopes the assistant relays to the user.

## Reconnect flow

If the user sees a `reconnect required` error envelope (refresh token
expired, revoked, or rotated upstream), they click Reconnect in
Console → Integrations.  Same OAuth flow as initial setup; the new
refresh token replaces the old one in Secrets.  No Console Secret
edits required.

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Employment Hero is not connected for this assistant.` | One or more of `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `REFRESH_TOKEN` is unset | Open Integrations modal; the listed `missing_secrets` show what's needed.  Either paste the OAuth credentials and click Connect, or click Reconnect if previously connected. |
| `Employment Hero refresh failed — reconnect required.` | Refresh token invalid (expired, revoked, or rotated) | User clicks Reconnect in Console → Integrations |
| EH consent screen rejects the redirect (`invalid_redirect_uri`, `redirect_uri_mismatch`, or "Application not configured") | The redirect URI registered in the customer's EH developer-portal app doesn't exactly match the URL Console sends | Re-open the Connect modal — Step 1 shows the canonical redirect URI for this console.  Copy it and paste into the EH app's Redirect URIs field exactly (no trailing slash, no port mismatch).  Save the EH app, then retry Connect. |
| `403` on specific capabilities | Customer's EH developer-portal app lacks that scope, or their EH role doesn't permit it | Edit the app's scope catalogue in EH developer portal, then click Reconnect.  See `employmenthero_tier_gating.md`. |
| `Multiple organisations may be available` hint | Token has access to >1 organisation and `ORGANISATION_ID` not set | Captured automatically by Connect; if missing, set manually via Settings → Secrets after calling `list_organisations(mock=False)`. |
| `429` rate-limit | Sync too aggressive | Increase `EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR` or reduce per-object cadence in `EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS`. |

## Region notes

Employment Hero operates in UK / AU / NZ / SG / MY.  The default
`api.employmenthero.com` host routes regionally at the load balancer
for most accounts.

If a UK customer hits region-mismatch errors (typically a 404 or 401
that doesn't reflect token validity), set
`EMPLOYMENTHERO_BASE_URL=https://api.employmenthero.co.uk` and retry.
The OAuth token endpoint is the same URL across regions.
