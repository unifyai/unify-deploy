# Valos Integration Package

Valos.ai property valuation connector — an AI-driven valuation platform
used by RICS-registered valuers to generate reports from trusted property
data sources.

> **Client-specific runtime, generic Console card.**  The runtime
> package is intentionally narrow — Valos is opted into by a single
> client deployment via the `integrations=[...]` list on
> `BASE_SPEC.derive(...)` (see e.g.
> `clients/clientzeta/deployments/v0/deployment.py`), and the intended
> consumer is the `clients/clientdelta/` subpackage which will land
> separately.  The Console-side `INTEGRATION_PROVIDERS` registry,
> however, lists Valos in the standard alphabetical catalogue
> (`id: 'valos'`, `auth.kind: 'api_key'`) so customers on any
> deployment can paste a `VALOS_API_KEY` without falling back to the
> freeform `custom` flow.  The runtime only acts on the secret when
> the deployment opts the package in; on deployments that don't, the
> secret simply sits unused.  No metadata on this package marks it as
> client-specific — that's purely emergent from which clients
> reference the slug.

> **Placeholder.** The Valos auth scheme and endpoint paths are not yet
> publicly documented.  The current `_client.py` assumes a single bearer
> API key and a configurable base URL as a stand-in.  Once the Valos
> developer team confirms the real contract (bearer / OAuth client
> credentials / signed-request / multi-field), harden `_client.py` to
> match the patterns in `hubspot/_client.py` (in-process token cache,
> 401 cache-bust, 429 backoff, structured error envelopes) and this
> tool inherits the change.

## Surface

- **Live API access** — `valos_request(method, path, params, body)`.
  The actor calls it directly for every Valos REST surface; endpoint
  paths are constructed against the live Valos API docs.

No sync orchestration.  No DataManager mirror.  No typed account-info
helper yet — add `get_valos_account_info` as the canonical "is the
integration working?" smoke test once a stable identity endpoint is
documented.

## Authentication

Placeholder bearer key.  One required secret:

- `VALOS_API_KEY` — bearer API key for the Valos API.  **Confirm
  credential type against the official Valos developer documentation
  before going to production.**

One optional override:

- `VALOS_API_BASE_URL` — defaults to `https://api.valos.ai`; set for
  sandbox / staging environments.

The Console exposes a `valos` card under
`src/constants/assistants/integrations.ts` (`auth.kind: 'api_key'`,
single sensitive field `VALOS_API_KEY`).  The base-URL override is
intentionally not surfaced in the dialog — it's rare enough to live via
the freeform `custom` provider, mirroring how Salto KS routes its
rare optional config (`SALTO_KS_BASE_URL`, etc.).  The ClientDelta
deployment also pre-seeds the key as part of its seed secrets, so
opted-in assistants are ready to call Valos without the customer
touching the Integrations tab.

## Conventions

Retry, rate-limit, and error-envelope handling are intentionally minimal
pending real API docs — failures bubble as a basic
`{"error": ..., "status_code": ...}` shape.  Once limits are known,
mirror the more elaborate behaviour in `hubspot/_client.py` and
`salto_ks/_client.py`:

- In-process access-token cache keyed on stable identifiers.
- 401 mid-flight busts the cache and retries once.
- 429 honours `Retry-After` with exponential backoff
  (`VALOS_RATE_LIMIT_MAX_RETRIES` / `VALOS_RATE_LIMIT_BACKOFF_FACTOR`).
- Structured envelopes: `_not_connected_envelope`, `_403_envelope`,
  `_credentials_rejected_envelope`.

For comparable reference implementations, see `hubspot/_client.py`
(API-key) and `salto_ks/_client.py` (OAuth ROPC).
