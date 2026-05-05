# Setup — Employment Hero Connection

The Connect flow is OAuth-based.  Customers create their own Employment
Hero developer-portal app, paste its `client_id` and `client_secret`
into Console Secrets, then click Connect once.  unity-deploy refreshes
access tokens behind the scenes from then on.

## Customer-side prerequisite — create an EH developer-portal app

1. Visit https://developer.employmenthero.com and create a new app.
2. Set the redirect URI to **`<your-console-domain>/oauth/employmenthero/callback`**
   (your Unify console URL — operator provides this).
3. Note the Client ID and Client Secret.  These are per-customer; never
   shared with other Unify deployments.

## Console-side setup (per assistant)

1. In Console → Settings → Secrets, add:
   * `EMPLOYMENTHERO_OAUTH_CLIENT_ID` — Client ID from step 3 above (not sensitive).
   * `EMPLOYMENTHERO_OAUTH_CLIENT_SECRET` — Client Secret from step 3 above (sensitive).
2. In Console → Integrations → Employment Hero, click **Connect**.
3. Grant consent on Employment Hero.
4. Done.  unity-deploy refreshes tokens automatically; the user reconnects
   via the same button if/when the refresh token expires (~60 days for
   most EH accounts).

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
