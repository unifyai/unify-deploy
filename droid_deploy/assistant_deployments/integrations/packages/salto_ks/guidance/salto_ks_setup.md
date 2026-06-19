# Setup — Salto KS Connection

Salto's documented "Backend Server" integration type uses OAuth 2.0
**Resource Owner Password Credentials (ROPC)**.  The customer needs
**two** things from Salto, not one:

1. An **OAuth client** (Client ID + Client Secret) issued by their
   regional Salto Business Unit.
2. A **Salto KS user account** (email + password) — best practice is
   a dedicated service-account user the customer creates in their KS
   dashboard.

Customer pastes all four values into chat or Console -> Settings ->
Secrets, and the integration is ready to use.

## End-to-end customer flow

### Step 1: Confirm product line and environment

The integration supports Salto KS (cloud).  Salto Space (on-prem) has
no cloud API and is unsupported; Salto Nebula needs separate package
work.  Ask the customer:

- Are you on Salto KS (cloud)?  Required.
- Production or acceptance/sandbox environment?  Acceptance is rarely
  used outside vendor pilots; production is the default.

### Step 2: Customer requests OAuth client from regional BU

Email regional Salto Business Unit (EU / US / AP) with a request like:

> *"Please issue Salto Connect API client credentials for the Backend
>  Server integration type, with the ``user_api.full_access`` scope,
>  for our [production / acceptance] environment in the [eu / us / ap]
>  region."*

Turnaround is typically days, not weeks.  The BU returns a Client ID
and a Client Secret.

### Step 3: Customer creates a service-account user in KS

In their Salto KS dashboard:

1. Create a dedicated user (e.g. email ``svc-droid@customer.com``).
2. Set a strong password.  This password is held long-term in
   SecretManager — communicate that to the customer before they pick
   one.
3. Grant the user only the KS roles the assistant actually needs.
   Over-privileged service accounts are a recurring source of incidents.
4. Record the email + password to paste into Console.

> **Why a service account?**  ROPC requires a real Salto user
> credential to be held by the integration.  Using a real person's
> login means: their password rotation breaks the integration; their
> KS audit trail mixes integration activity with their own; off-
> boarding them disables the integration.  Dedicated service-account
> users avoid all three.

### Step 4: Customer pastes the four credentials

Two paths, both end up in SecretManager:

- **Via chat** (current default): customer pastes the four values
  one-by-one to the assistant.
- **Via Console -> Settings -> Integrations -> Salto KS**: opens a
  dialog with four labeled fields (Client ID, Client Secret, Service
  Account Email, Service Account Password).

The runtime defaults to EU production hosts
(``identity.eu.my-clay.com`` + ``user-api.eu.my-clay.com``).  For
sandbox (acceptance), non-EU regions, or any BU-issued non-standard
host, set ``SALTO_KS_IDENTITY_HOST`` (and usually
``SALTO_KS_BASE_URL``) via Console -> Settings -> Secrets ->
Custom — see the Optional secrets table below.

### Step 5: Verify the connection

```
get_salto_account_info(mock=False)
```

On success, returns the installation metadata.  On failure, returns
a structured envelope describing the issue (missing secrets, wrong
credentials, scope problem, wrong identity host).

## Required secrets

| Secret | Required? | Purpose |
|---|---|---|
| `SALTO_KS_CLIENT_ID` | yes | OAuth client ID from regional BU |
| `SALTO_KS_CLIENT_SECRET` | yes | OAuth client secret from regional BU |
| `SALTO_KS_USERNAME` | yes | Email of the KS service-account user |
| `SALTO_KS_PASSWORD` | yes | Password of the KS service-account user |

## Optional secrets

| Secret | Purpose |
|---|---|
| `SALTO_KS_DEFAULT_SITE_ID` | Default site to scope list calls to |
| `SALTO_KS_OAUTH_SCOPES` | Override scope string (default `user_api.full_access`) |
| `SALTO_KS_IDENTITY_HOST` | Identity-server host override (origin only).  Default `https://identity.eu.my-clay.com` (EU production).  Set for sandbox (`https://identity-acc.eu.my-clay.com`), non-EU regional clouds, or any BU-issued non-standard host |
| `SALTO_KS_BASE_URL` | API host override.  Default `https://user-api.eu.my-clay.com` is a best guess — verify with BU and set explicitly for non-EU regions or sandbox |
| `SALTO_KS_OAUTH_TOKEN_URL` | Full token-endpoint URL override (host + path).  Takes precedence over `SALTO_KS_IDENTITY_HOST` |
| `SALTO_KS_REQUEST_TIMEOUT_SECONDS` / `_RATE_LIMIT_MAX_RETRIES` / `_RATE_LIMIT_BACKOFF_FACTOR` | HTTP behaviour (`30` / `3` / `1.5`) |

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Salto KS is not connected for this assistant.` | One or more of the four required secrets missing | Customer pastes the missing values |
| `Salto KS token endpoint rejected the credentials.` (400/401) | Wrong client creds, wrong service-account creds, or the customer's credentials live on a non-EU-production identity server | Re-check each pair against the BU email and the KS dashboard; if the BU issued credentials for sandbox / non-EU, set `SALTO_KS_IDENTITY_HOST` (and usually `SALTO_KS_BASE_URL`) |
| 403 on a specific resource | Service-account user's KS role doesn't permit it | Customer's KS admin grants the service-account user the required role |
| `429` rate-limit | Too many calls per second | Increase `SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR` |
| Connection-refused or DNS errors on API calls | Wrong API base URL (default is a guess) | Confirm the API host with the regional BU and set `SALTO_KS_BASE_URL` |

## Credential rotation

Three rotation paths:

- **OAuth client rotation** (BU re-issues `CLIENT_ID` / `CLIENT_SECRET`):
  customer re-pastes via chat or Console.  Token cache busts on the
  next 401.
- **Service-account password rotation** (KS-side): customer re-pastes
  `SALTO_KS_PASSWORD`.  Token cache busts on the next 401.
- **Service-account email change**: shouldn't happen in practice —
  emails are stable identifiers.  If it does, treat as a fresh setup.

## Open verification items (from BU during onboarding)

The Salto public docs as of 2026-05 don't explicitly document:

- The actual API base URL (we default to a best-guess pattern).
- Whether US/AP regional identity hosts follow the same naming as EU.
- Token TTL for ROPC (we assume ~1h).
- Refresh-token support for the Backend Server flow (the docs only
  mention `offline_access` for interactive flows).
- Pagination shape on list endpoints.

The BU email above can include these as questions alongside the
credentials request.
