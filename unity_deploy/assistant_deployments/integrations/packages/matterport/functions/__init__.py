"""Matterport callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth (HTTP Basic with Token ID + Secret), retry,
  ``matterport_graphql`` HTTP helper.
* ``_config`` — runtime config dict resolved from env.
* ``_capabilities`` — token capability probe used by
  ``probe_matterport_tier``.
* ``_local_helpers`` — DataManager-query helpers used by ``local``.
* ``_sync_helpers`` — DataManager-write helpers (incl.
  ``MODEL_FIELDS_FRAGMENT``) used by sync.
* ``_sync_models`` / ``_sync_view_stats`` — per-object snapshot
  writers, dispatched by the orchestrator in ``sync``.

Discoverable (registered) modules:

* ``live`` — single ``matterport_graphql_query`` tool covering the
  live GraphQL API.
* ``embed`` — Showcase URL builder (browser-tier; no API call).
* ``linking`` — DataManager-side cross-app joins (model<->unit,
  views<->HubSpot leads).
* ``local`` — ``query_local_matterport_*`` analytical queries.
* ``sync`` — orchestrator + state inspection + tier probe.
"""
