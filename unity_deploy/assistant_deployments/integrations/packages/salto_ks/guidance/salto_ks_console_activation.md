# Console Activation — Salto KS

Once the four required credentials have been pasted (via chat or
Console -> Settings -> Secrets), the admin verifies the connection via
Console.  This document is the runbook the admin follows.

> **Phase note.**  The Console UI surface that drives this runbook is
> the shared `ApiKeyIntegrationDialog` card; Salto KS is wired in.
> There is no per-integration sync activation toggle for Salto KS
> because the package is live-only — no DataManager mirror, no sync
> orchestrator.

## Runbook

1. **Open Console -> Settings -> Integrations -> Salto KS.**
2. **Verify the secrets landed.**  The dialog covers the four required
   fields and two optional ones:
   - **Client ID** (required)
   - **Client secret** (required)
   - **Service account email** (required) — the KS user the customer
     created
   - **Service account password** (required)
   - **Region** (optional) — `eu` (default) / `us` / `ap`
   - **Environment** (optional) — `prod` (default) / `acc`
3. **Smoke test from chat.**  No "Test connection" button exists in
   the Console (matching the pattern for every other integration).
   Run from chat instead:
   ```
   get_salto_account_info(mock=False)
   ```
   - Returns the installation metadata → connection works.
   - Returns a structured envelope describing the issue → re-paste
     corrected values.

## When the customer reports issues

- **"Nothing's working."**  Run `get_salto_account_info(mock=False)`
  in chat.
  - "Not connected" envelope → one or more of the four required
    secrets is missing; re-paste.
  - "Token endpoint rejected the credentials" (400/401) → wrong
    client creds, wrong service-account creds, environment mismatch
    (acc vs prod), or region mismatch.  Read the envelope hint, work
    through the four causes.
  - Installation metadata returned → auth is fine; the issue is
    downstream.

- **"I get 403s on specific operations."**  The service-account
  user's KS role doesn't permit the resource.  The customer's KS
  admin grants the user the missing role in their KS dashboard; no
  re-paste needed in Console.  (This is different from an OAuth
  scope problem — Salto's Backend Server flow uses the single coarse
  scope `user_api.full_access`, so 403s are almost always KS-role
  issues.)

- **"Connection-refused or DNS errors on API calls."**  The default
  API base URL (`user-api.<region>.my-clay.com`) is a best guess
  extrapolated from Salto's documented identity host pattern.  If the
  BU told the customer a different host, set `SALTO_KS_BASE_URL` via
  Console -> Settings -> Secrets.

## Reconnect

Salto KS has no "reconnect" concept — ROPC has no refresh token, no
user-consent step, no callback.  Three rotation events the customer
might hit:

- **OAuth client rotated** by the BU → re-paste `SALTO_KS_CLIENT_ID`
  + `SALTO_KS_CLIENT_SECRET`.
- **Service-account password rotated** in KS → re-paste
  `SALTO_KS_PASSWORD`.
- **Service-account user disabled** in KS → integration goes silent;
  customer either re-enables the user or creates a new one and
  re-pastes `SALTO_KS_USERNAME` + `SALTO_KS_PASSWORD`.

In every case the in-process token cache busts on the next 401
mid-flight; no manual Console action required.
