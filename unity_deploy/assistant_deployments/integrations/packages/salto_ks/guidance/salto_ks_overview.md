# Salto KS Integration — Overview

Reusable Salto KS (cloud) connector for physical access control.  Use
when:

- Looking up users, sites, locks, or access events in Salto.
- Reasoning about who holds physical access to which doors / sites.
- Cross-referencing access events with HR / property records to answer
  "was contractor X on site at the time they said?" or "which residents
  have key fobs?"

## What this package is and isn't

- **Salto KS (cloud)** — supported.  REST + the documented "Backend
  Server" integration type.
- **Salto Space (on-prem)** — NOT supported.  No cloud API exists for
  Space; integration would mean SHIP/XML files or middleware.  Confirm
  the customer is on KS before promising anything.
- **Salto Nebula (newer platform)** — not supported in this package.
  Nebula has its own API surface with potentially different scopes /
  endpoints; will need separate package work.

## Authentication model

Salto's documented "Backend Server" integration uses **OAuth 2.0
Resource Owner Password Credentials (ROPC)** layered with **OpenID
Connect**.  Each token-mint request sends:

- ``Authorization: Basic <base64(client_id:client_secret)>`` — the
  OAuth client identity, issued by the customer's regional Salto
  Business Unit (EU/US/AP).
- Body: ``grant_type=password``, ``username=<KS email>``,
  ``password=<KS password>``, ``scope=user_api.full_access``.

Four secrets are required:

1. ``SALTO_KS_CLIENT_ID`` + ``SALTO_KS_CLIENT_SECRET`` — the OAuth
   client.  Issued by the regional BU.
2. ``SALTO_KS_USERNAME`` + ``SALTO_KS_PASSWORD`` — a Salto KS user
   account (the resource owner under ROPC).  Best practice is a
   **dedicated service-account user** the customer creates in their
   KS dashboard.

Tokens are minted server-side at call time and cached in-process for
~1 hour.  No refresh token, no user-consent dance, no callback URL.

> **Security note.**  ROPC means the assistant holds a Salto user
> password long-term in SecretManager.  Tell the customer to create
> a dedicated service-account user (not their personal login) and
> grant it only the KS roles the assistant actually needs.  Standard
> OAuth ROPC criticisms apply otherwise.

## What's NOT integrated

- **DataManager mirror** — none.  Salto operations affect physical
  doors and every read is best-served fresh from the API.
- **Sync orchestrator** — none, by design.  See above.
- **Webhooks** — out of scope; not advertised in Salto's public docs.
- **Bulk pulls** — ``salto_request`` returns first page only; the
  actor re-issues with the cursor from ``links.next`` for subsequent
  pages.

## Cross-system linking via `external_id` on Salto users

Salto users carry an operator-controlled ``external_id`` field.  The
recommended convention when provisioning a Salto user from another
integrated system is:

```
external_id = "<source_system>:<source_id>"
```

Examples:

- ``realpage:res_12345`` — a RealPage resident
- ``employment_hero:emp_789`` — an Employment Hero employee
- ``vantify:contractor_4567`` — a Vantify Supply Chain accredited
  contractor

This makes joins back to the source system unambiguous: the assistant
can filter Salto users by the ``realpage:`` prefix to find every
resident with a Salto user record, and pull the resident ID by
stripping the prefix.

Joins across packages are an assistant-level concern, not a
package-level one — each package stays self-contained.
