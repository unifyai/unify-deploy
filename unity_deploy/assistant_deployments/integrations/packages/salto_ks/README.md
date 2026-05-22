# Salto KS Integration Package

Salto KS (cloud) connector for physical access control.  Live API
access goes through a single ``salto_request`` tool that exposes the
authenticated REST client to the actor; account / installation
introspection is kept as a typed connectivity smoke test.

## Surface

- **Account** — `get_salto_account_info`, `list_salto_installations`.
  Typed wrappers; use the first as the canonical "is the integration
  working?" smoke test.
- **Everything else (live)** — `salto_request(method, path, params,
  body)`.  The actor calls it directly for users, sites, locks,
  access events, access rights, credentials, time schedules, etc.  See
  Salto's API docs for paths.  First-page only — re-issue with the
  cursor from `links.next` for subsequent pages.

## Authentication

Salto's documented "Backend Server" integration type — OAuth 2.0
**Resource Owner Password Credentials (ROPC)** layered with OpenID
Connect.  Four secrets required:

- `SALTO_KS_CLIENT_ID` + `SALTO_KS_CLIENT_SECRET` — OAuth client
  identity issued by the customer's regional Salto Business Unit
  (EU / US / AP).  Sent as HTTP Basic auth.
- `SALTO_KS_USERNAME` + `SALTO_KS_PASSWORD` — Salto KS user account
  (the resource owner under ROPC).  Best practice: a dedicated
  service-account user the customer creates in their KS dashboard.
  Sent on the token-mint POST body.

Customer pastes all four into chat (or Console -> Settings -> Secrets);
the runtime mints short-lived bearer tokens server-side at call time.
No user-consent dance, no callback URL, no refresh token — when the
~1h access token expires, the runtime re-mints from the cached
credentials.  See `guidance/salto_ks_setup.md` for the end-to-end
customer flow including service-account creation.

Endpoints default to EU production (`identity.eu.my-clay.com` +
`user-api.eu.my-clay.com`).  Sandbox (acceptance), non-EU regional
clouds, and any BU-issued non-standard host are reached by setting
`SALTO_KS_IDENTITY_HOST` (and usually `SALTO_KS_BASE_URL`) as
custom secrets — see `guidance/salto_ks_setup.md`.  Salto Space
(on-prem) has no cloud API and is out of scope — confirm the customer
is on Salto KS before scoping.

## Conventions

On-demand functions default to `mock=True` for safe testing; live
calls require all four required secrets to be set.  401 from the API
mid-flight busts the in-process token cache and retries once.  403
returns a structured envelope pointing at the customer's KS role
configuration — under ROPC + the single coarse `user_api.full_access`
scope, 403s almost always indicate the service-account user's KS
role is too narrow rather than an OAuth scope gap.  The package never
raises on capability gating.

For setup, see `guidance/salto_ks_setup.md`.
For the admin Console runbook, see `guidance/salto_ks_console_activation.md`.
